"""Streaming parser for Wikimedia's MySQL dump files.

Wikimedia ships each table as a gzipped MySQL script: a ``CREATE TABLE``
statement followed by long runs of ``INSERT INTO `t` VALUES (..),(..),..;``.
DuckDB cannot read that, so every stage that touches a raw dump comes through
here.

Three design choices are load-bearing:

* **Column order comes from the DDL, never from a hardcoded position.** The
  ``CREATE TABLE`` statement sits at the top of the same file. Column sets have
  changed across MediaWiki versions -- ``pagelinks`` losing ``pl_namespace`` and
  ``pl_title`` is exactly that hazard -- and a positional parser would read the
  wrong field silently instead of failing.

* **Values come back as raw ``bytes``, never decoded or cast.** The caller
  decides what a UTF-8 failure means and what ``b"1"`` means, which keeps policy
  in the stage where it is visible and keeps this module free of work the caller
  may not want.

* **Parsing is per value, not per byte.** The scanner jumps between delimiters
  with ``bytes.find`` and a compiled regex, both of which run at C speed, so
  Python-level work happens once per value rather than once per character.

Usage::

    reader = open_dump(path)
    for page_id, title in reader.rows("page_id", "page_title"):
        ...

Each call to :meth:`DumpReader.rows` is an independent pass over the file.
"""

from __future__ import annotations

import gzip
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Final

#: How much to read per block. Individual tuples are far smaller than this, so
#: a tuple straddling a boundary is rare and costs one re-scan when it happens.
DEFAULT_BLOCK: Final = 1 << 20

#: The DDL always precedes the first INSERT, so this much of the head is plenty.
_HEAD_LIMIT: Final = 1 << 20

_GZIP_MAGIC: Final = b"\x1f\x8b"


class DumpFormatError(Exception):
    """The dump did not look like a MySQL dump we understand."""


class _NeedMore(Exception):
    """Internal control flow: the buffer ends mid-token, so read another block."""


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #

_CREATE_RE: Final = re.compile(rb"CREATE\s+TABLE\s+`([^`]+)`\s*\(", re.IGNORECASE)

# A column definition is a line that *starts* with a backtick-quoted identifier.
# Constraint lines never do -- they start with PRIMARY / UNIQUE / KEY / INDEX /
# CONSTRAINT / FULLTEXT -- so this one rule separates the two cleanly, even when
# an index happens to share a column's name (`KEY `page_random` (`page_random`)`).
_COLUMN_RE: Final = re.compile(rb"^[ \t]*`([^`]+)`[ \t]+\S", re.MULTILINE)

_INSERT_RE: Final = re.compile(
    rb"INSERT\s+INTO\s+`([^`]+)`\s*(\([^)]*\))?\s*VALUES\s*", re.IGNORECASE
)


@dataclass(frozen=True)
class TableSchema:
    """The table name and column order taken from the dump's own DDL."""

    name: str
    columns: tuple[str, ...]

    def index_of(self, column: str) -> int:
        try:
            return self.columns.index(column)
        except ValueError:
            raise DumpFormatError(
                f"table `{self.name}` has no column {column!r}; it has {list(self.columns)}"
            ) from None


def parse_schema(head: bytes) -> TableSchema:
    """Extract the table name and column order from a dump's ``CREATE TABLE``."""
    match = _CREATE_RE.search(head)
    if match is None:
        raise DumpFormatError("no CREATE TABLE statement found in the dump head")
    name = match.group(1).decode("ascii", "replace")

    end = head.find(b";", match.end())
    body = head[match.end() : end if end != -1 else len(head)]
    columns = tuple(m.group(1).decode("ascii", "replace") for m in _COLUMN_RE.finditer(body))
    if not columns:
        raise DumpFormatError(f"no column definitions found in CREATE TABLE `{name}`")
    return TableSchema(name=name, columns=columns)


# --------------------------------------------------------------------------- #
# Value scanning
# --------------------------------------------------------------------------- #

_QUOTE: Final = 0x27  # '
_BACKSLASH: Final = 0x5C  # \
_LPAREN: Final = 0x28  # (
_RPAREN: Final = 0x29  # )
_COMMA: Final = 0x2C  # ,
_SPACE: Final = b" \t\r\n"

_BARE_END: Final = re.compile(rb"[,)]")

# MySQL string escapes. Anything not listed collapses to the escaped character
# itself, except \% and \_ which keep their backslash (see _KEEP_BACKSLASH).
_ESCAPES: Final = {
    0x30: b"\x00",  # \0
    0x27: b"'",
    0x22: b'"',
    0x62: b"\x08",  # \b
    0x6E: b"\n",
    0x72: b"\r",
    0x74: b"\t",
    0x5A: b"\x1a",  # \Z
    0x5C: b"\\",
}
_KEEP_BACKSLASH: Final = frozenset({0x25, 0x5F})  # % and _


def unescape(raw: bytes) -> bytes:
    """Decode the MySQL escapes inside a quoted string's body.

    Fast path: most values contain no backslash and no doubled quote, so two
    C-speed scans let us hand the slice straight back.
    """
    if _BACKSLASH not in raw and b"''" not in raw:
        return raw
    out = bytearray()
    i, n = 0, len(raw)
    while i < n:
        char = raw[i]
        if char == _BACKSLASH and i + 1 < n:
            nxt = raw[i + 1]
            if nxt in _KEEP_BACKSLASH:
                out += b"\\"
                out.append(nxt)
            else:
                mapped = _ESCAPES.get(nxt)
                # An unlisted escape collapses to the character itself.
                out += mapped if mapped is not None else bytes((nxt,))
            i += 2
        elif char == _QUOTE and i + 1 < n and raw[i + 1] == _QUOTE:
            out += b"'"
            i += 2
        else:
            out.append(char)
            i += 1
    return bytes(out)


def _scan_quoted(buf: bytes, start: int) -> tuple[bytes, int]:
    """Scan ``buf[start] == b"'"``. Returns the still-escaped body and the next position.

    A quote ends the string only if an even number of backslashes precedes it
    and it is not the first half of a doubled ``''``. Both checks are why a
    title like ``Trailing\\`` or ``Doubled''Quote`` survives intact.
    """
    body = start + 1
    probe = body
    while True:
        close = buf.find(b"'", probe)
        if close < 0:
            raise _NeedMore
        back = close - 1
        while back >= body and buf[back] == _BACKSLASH:
            back -= 1
        if (close - 1 - back) % 2:
            probe = close + 1  # escaped quote, keep going
            continue
        if close + 1 >= len(buf):
            # Cannot yet tell a closing quote from the first half of a ''.
            raise _NeedMore
        if buf[close + 1] == _QUOTE:
            probe = close + 2  # doubled '' is a literal quote
            continue
        return buf[body:close], close + 1


def _scan_bare(buf: bytes, start: int) -> tuple[bytes | None, int]:
    """Scan an unquoted value: ``NULL``, an integer, a float, or a ``0x..`` literal."""
    match = _BARE_END.search(buf, start)
    if match is None:
        raise _NeedMore
    token = buf[start : match.start()].strip()
    return (None if token.upper() == b"NULL" else token), match.start()


def _skip_space(buf: bytes, i: int) -> int:
    n = len(buf)
    while i < n and buf[i] in _SPACE:
        i += 1
    if i >= n:
        raise _NeedMore
    return i


def _scan_tuple(buf: bytes, start: int, ncols: int) -> tuple[list[bytes | None], int]:
    """Scan one ``(v1,v2,..)`` tuple. Raises :class:`_NeedMore` if it is incomplete."""
    i = _skip_space(buf, start)
    if buf[i] != _LPAREN:
        raise DumpFormatError(f"expected '(' at offset {i}, found {buf[i : i + 24]!r}")
    i += 1

    values: list[bytes | None] = []
    while True:
        i = _skip_space(buf, i)
        if buf[i] == _RPAREN:  # only reachable for an empty tuple
            i += 1
            break
        if buf[i] == _QUOTE:
            raw, i = _scan_quoted(buf, i)
            values.append(unescape(raw))
        else:
            value, i = _scan_bare(buf, i)
            values.append(value)

        i = _skip_space(buf, i)
        if buf[i] == _COMMA:
            i += 1
            continue
        if buf[i] == _RPAREN:
            i += 1
            break
        raise DumpFormatError(f"expected ',' or ')' at offset {i}, found {buf[i : i + 24]!r}")

    if len(values) != ncols:
        raise DumpFormatError(f"row has {len(values)} values but the DDL declares {ncols} columns")
    return values, i


def _scan_separator(buf: bytes, start: int) -> tuple[int, int]:
    """After a tuple, expect ``,`` (another follows) or ``;`` (statement over)."""
    i = _skip_space(buf, start)
    char = buf[i]
    if char in (_COMMA, 0x3B):  # , or ;
        return char, i + 1
    raise DumpFormatError(f"expected ',' or ';' at offset {i}, found {buf[i : i + 24]!r}")


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


def _open(path: Path) -> IO[bytes]:
    """Open gzipped or plain SQL, decided by magic bytes rather than the suffix."""
    with path.open("rb") as probe:
        magic = probe.read(2)
    if magic == _GZIP_MAGIC:
        return gzip.open(path, "rb")
    return path.open("rb")


class _Window:
    """A sliding read window over the decompressed stream.

    Consumed bytes are dropped on each refill, so the buffer stays bounded while
    ``pos`` remains valid for the caller after a refill.
    """

    def __init__(self, fh: IO[bytes], block_size: int) -> None:
        self._fh = fh
        self._block = block_size
        self.data = b""
        self.pos = 0
        self.at_eof = False

    def fill(self) -> bool:
        """Read one more block, compacting first. False once the stream is spent."""
        chunk = self._fh.read(self._block)
        if not chunk:
            self.at_eof = True
            return False
        if self.pos:
            self.data = self.data[self.pos :]
            self.pos = 0
        self.data += chunk
        return True


class DumpReader:
    """Streams rows out of one dump file."""

    def __init__(self, path: Path | str, *, block_size: int = DEFAULT_BLOCK) -> None:
        if block_size < 1:
            raise ValueError("block_size must be at least 1")
        self.path = Path(path)
        self.block_size = block_size
        self.schema = parse_schema(self._head())

    def _head(self) -> bytes:
        with _open(self.path) as fh:
            head = b""
            while len(head) < _HEAD_LIMIT:
                chunk = fh.read(1 << 16)
                if not chunk:
                    break
                head += chunk
                if b"INSERT INTO" in head:
                    break
            return head

    def rows(self, *columns: str) -> Iterator[tuple[bytes | None, ...]]:
        """Yield one tuple per row, projected to *columns* in the order given.

        With no columns, yields every column in DDL order. Values are raw
        ``bytes``, or ``None`` for SQL ``NULL``.
        """
        if not columns:
            yield from (tuple(values) for values in self._tuples())
            return
        picks = [self.schema.index_of(name) for name in columns]
        for values in self._tuples():
            yield tuple(values[i] for i in picks)

    def _tuples(self) -> Iterator[list[bytes | None]]:
        ncols = len(self.schema.columns)
        with _open(self.path) as fh:
            window = _Window(fh, self.block_size)
            in_values = False
            while True:
                if not in_values:
                    match = _INSERT_RE.search(window.data, window.pos)
                    if match is None:
                        if not window.fill():
                            return
                        continue
                    if match.group(2):
                        raise DumpFormatError(
                            "INSERT with an explicit column list is not supported; "
                            "this parser trusts the CREATE TABLE column order"
                        )
                    table = match.group(1).decode("ascii", "replace")
                    if table != self.schema.name:
                        raise DumpFormatError(
                            f"INSERT INTO `{table}` but the DDL declared `{self.schema.name}`"
                        )
                    window.pos = match.end()
                    in_values = True
                    continue

                # Retry from the tuple's start after a refill, so a tuple split
                # across a block boundary costs one re-scan and nothing else.
                try:
                    values, nxt = _scan_tuple(window.data, window.pos, ncols)
                except _NeedMore:
                    if not window.fill():
                        raise DumpFormatError("dump ends inside an INSERT statement") from None
                    continue
                window.pos = nxt
                yield values

                while True:
                    try:
                        separator, nxt = _scan_separator(window.data, window.pos)
                    except _NeedMore:
                        if window.fill():
                            continue
                        # A dump whose final statement lacks its ';'.
                        return
                    break
                window.pos = nxt
                if separator == 0x3B:  # ;
                    in_values = False


def open_dump(path: Path | str, *, block_size: int = DEFAULT_BLOCK) -> DumpReader:
    """Open a dump and read its schema. See :class:`DumpReader`."""
    return DumpReader(path, block_size=block_size)
