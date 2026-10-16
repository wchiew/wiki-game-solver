"""Per-stage counters, appended to ``work/stats.jsonl``.

Every stage records what came in, what went out, and what it dropped and why.
Those numbers are what stage 9's drift check and ``manifest.json`` are built
from, so a stage that quietly starts dropping rows is visible rather than
mysterious.

A record is written even when the stage raises, because a failed stage's
counters are usually the most interesting ones.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .paths import BuildPaths


@dataclass
class StageStats:
    """Mutable counters for one stage run."""

    stage: str
    rows_in: int = 0
    rows_out: int = 0
    dropped: Counter[str] = field(default_factory=Counter)
    extra: dict[str, Any] = field(default_factory=dict)

    def drop(self, reason: str, count: int = 1) -> None:
        """Record *count* input rows discarded for *reason*."""
        self.dropped[reason] += count

    @property
    def total_dropped(self) -> int:
        return sum(self.dropped.values())

    def as_record(self, *, seconds: float, ok: bool) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "seconds": round(seconds, 3),
            "ok": ok,
            "rows_in": self.rows_in,
            "rows_out": self.rows_out,
            "dropped": dict(sorted(self.dropped.items())),
            **self.extra,
        }


@contextmanager
def stage_stats(paths: BuildPaths, stage: str) -> Iterator[StageStats]:
    """Time a stage and append one JSONL record, success or failure."""
    stats = StageStats(stage=stage)
    started = time.perf_counter()
    ok = False
    try:
        yield stats
        ok = True
    finally:
        record = stats.as_record(seconds=time.perf_counter() - started, ok=ok)
        paths.work.mkdir(parents=True, exist_ok=True)
        with paths.stats_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")


def read_stats(paths: BuildPaths) -> list[dict[str, Any]]:
    """Every record written for this build, oldest first."""
    if not paths.stats_file.exists():
        return []
    with paths.stats_file.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]
