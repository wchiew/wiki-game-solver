"""Filesystem layout for one build.

Every stage takes its paths from :class:`BuildPaths` rather than assembling its
own, so the directory convention lives in exactly one place and is directly
testable.

    data/<wiki>/<date>/raw/      downloaded dumps, untouched
    data/<wiki>/<date>/work/     intermediates (Parquet, chunks, stats)
    graphs/<date>/               published artifacts (stage 10)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: The four dump tables the pipeline needs, all from the same dump run.
TABLES = ("page", "pagelinks", "linktarget", "redirect")

#: A graph version is the dump date, always ``YYYYMMDD``.
DATE_RE = re.compile(r"^\d{8}$")

#: Accepted as a date on the command line; stage 1 resolves it to a real date.
LATEST = "latest"


class InvalidDate(ValueError):
    """The date was neither ``YYYYMMDD`` nor resolvable."""


def check_date(date: str) -> str:
    """Return *date* if it is a pinned ``YYYYMMDD``, else raise."""
    if date == LATEST:
        raise InvalidDate(
            "this stage needs a pinned dump date, not 'latest'; run "
            "`wikigraph fetch --date latest` first and pass the date it prints"
        )
    if not DATE_RE.match(date):
        raise InvalidDate(f"expected a dump date as YYYYMMDD, got {date!r}")
    return date


def dump_filename(wiki: str, date: str, table: str) -> str:
    """The name Wikimedia gives a table's dump, e.g. ``simplewiki-20261001-page.sql.gz``.

    Free-standing because stage 1 needs it before a date is pinned, and so
    before a :class:`BuildPaths` can exist.
    """
    if table not in TABLES:
        raise ValueError(f"unknown dump table {table!r}; expected one of {TABLES}")
    return f"{wiki}-{date}-{table}.sql.gz"


@dataclass(frozen=True)
class BuildPaths:
    """Paths for a single ``(wiki, date)`` build."""

    root: Path
    wiki: str
    date: str

    def __post_init__(self) -> None:
        check_date(self.date)
        if not self.wiki:
            raise ValueError("wiki must be a non-empty name such as 'simplewiki'")

    @classmethod
    def of(cls, root: Path | str, wiki: str, date: str) -> BuildPaths:
        return cls(root=Path(root), wiki=wiki, date=date)

    @property
    def base(self) -> Path:
        return self.root / self.wiki / self.date

    @property
    def raw(self) -> Path:
        return self.base / "raw"

    @property
    def work(self) -> Path:
        return self.base / "work"

    @property
    def dumpstatus(self) -> Path:
        return self.raw / "dumpstatus.json"

    @property
    def stats_file(self) -> Path:
        return self.work / "stats.jsonl"

    def dump_filename(self, table: str) -> str:
        return dump_filename(self.wiki, self.date, table)

    def raw_file(self, table: str) -> Path:
        return self.raw / self.dump_filename(table)

    def work_file(self, name: str) -> Path:
        return self.work / name

    def ensure(self) -> BuildPaths:
        """Create the ``raw`` and ``work`` directories. Idempotent."""
        self.raw.mkdir(parents=True, exist_ok=True)
        self.work.mkdir(parents=True, exist_ok=True)
        return self
