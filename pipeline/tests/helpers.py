"""Shared test helpers.

Imported as ``helpers`` -- pytest puts the test directory on ``sys.path`` because
there is no ``__init__.py`` here.
"""

from __future__ import annotations

import gzip
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "fixtures"
TINY = FIXTURES / "tiny"

#: A minimal two-column table for focused parser tests.
DDL = b"""DROP TABLE IF EXISTS `t`;
CREATE TABLE `t` (
  `a` int(10) unsigned NOT NULL,
  `b` varbinary(255) DEFAULT NULL,
  PRIMARY KEY (`a`)
) ENGINE=InnoDB DEFAULT CHARSET=binary;
"""


def make_dump(
    directory: Path,
    body: bytes,
    *,
    ddl: bytes = DDL,
    name: str = "t.sql",
    gz: bool = False,
) -> Path:
    """Write a synthetic dump. Plain by default; the parser sniffs magic bytes."""
    raw = ddl + body
    path = directory / (name + ".gz" if gz else name)
    path.write_bytes(gzip.compress(raw) if gz else raw)
    return path


def values(*tuples: bytes, table: bytes = b"t") -> bytes:
    """Build one ``INSERT INTO `t` VALUES (..),(..);`` statement."""
    return b"INSERT INTO `" + table + b"` VALUES " + b",".join(tuples) + b";\n"


def page_ddl() -> bytes:
    """The real-shaped ``page`` DDL, taken from the committed fixture.

    Reading it out of the fixture keeps one source of truth, so a synthetic
    dump built here always has the same 12 columns the fixture declares.
    """
    text = (TINY / "page.sql").read_bytes()
    return text[: text.index(b"INSERT INTO")]


def page_row(page_id: int, namespace: int, title: bytes, *, is_redirect: int = 0) -> bytes:
    """One ``page`` tuple with plausible filler for the nine columns we ignore."""
    return (
        b"("
        + str(page_id).encode()
        + b","
        + str(namespace).encode()
        + b",'"
        + title
        + b"',"
        + str(is_redirect).encode()
        + b",0,0.5,'20260101000000',NULL,1,10,'wikitext',NULL)"
    )


def write_page_dump(dest: Path, *rows: bytes) -> Path:
    """Gzip a synthetic ``page`` dump containing *rows* to *dest*."""
    body = page_ddl() + values(*rows, table=b"page")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(gzip.compress(body))
    return dest


def mysql_quote(raw: bytes) -> bytes:
    """Encode bytes as a MySQL string literal, the way mysqldump does.

    The inverse of the parser's unescaping, which is what makes the Hypothesis
    round-trip test meaningful.
    """
    escapes = {
        0x00: rb"\0",
        0x08: rb"\b",
        0x0A: rb"\n",
        0x0D: rb"\r",
        0x1A: rb"\Z",
        0x22: rb"\"",
        0x27: rb"\'",
        0x5C: b"\\\\",
    }
    out = bytearray(b"'")
    for byte in raw:
        mapped = escapes.get(byte)
        if mapped is None:
            out.append(byte)
        else:
            out += mapped
    out += b"'"
    return bytes(out)
