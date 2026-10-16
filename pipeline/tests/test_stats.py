from __future__ import annotations

from pathlib import Path

import pytest

from wikigraph.paths import BuildPaths
from wikigraph.stats import read_stats, stage_stats


@pytest.fixture
def paths(tmp_path: Path) -> BuildPaths:
    return BuildPaths.of(tmp_path, "simplewiki", "20260101")


def test_records_a_successful_stage(paths: BuildPaths) -> None:
    with stage_stats(paths, "demo") as stats:
        stats.rows_in = 10
        stats.rows_out = 8
        stats.extra["note"] = "hello"

    record = read_stats(paths)[-1]
    assert record["stage"] == "demo"
    assert record["ok"] is True
    assert record["rows_in"] == 10
    assert record["rows_out"] == 8
    assert record["note"] == "hello"
    assert record["seconds"] >= 0
    assert record["finished_at"].endswith("+00:00")


def test_records_a_failed_stage(paths: BuildPaths) -> None:
    """A failed stage's counters are usually the interesting ones."""
    with pytest.raises(RuntimeError, match="boom"), stage_stats(paths, "demo") as stats:
        stats.rows_in = 5
        raise RuntimeError("boom")

    record = read_stats(paths)[-1]
    assert record["ok"] is False
    assert record["rows_in"] == 5


def test_drop_counters_accumulate(paths: BuildPaths) -> None:
    with stage_stats(paths, "demo") as stats:
        stats.drop("namespace")
        stats.drop("namespace", 2)
        stats.drop("decode_error")
        assert stats.total_dropped == 4

    assert read_stats(paths)[-1]["dropped"] == {"decode_error": 1, "namespace": 3}


def test_a_stage_that_drops_nothing_records_an_empty_map(paths: BuildPaths) -> None:
    with stage_stats(paths, "demo"):
        pass
    assert read_stats(paths)[-1]["dropped"] == {}


def test_records_append_in_order(paths: BuildPaths) -> None:
    for stage in ("fetch", "pages", "pages"):
        with stage_stats(paths, stage):
            pass
    assert [r["stage"] for r in read_stats(paths)] == ["fetch", "pages", "pages"]


def test_no_stats_file_yet(paths: BuildPaths) -> None:
    assert read_stats(paths) == []


def test_creates_the_work_directory(paths: BuildPaths) -> None:
    assert not paths.work.exists()
    with stage_stats(paths, "demo"):
        pass
    assert paths.stats_file.is_file()
