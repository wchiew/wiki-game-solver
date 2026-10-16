from __future__ import annotations

from pathlib import Path

import pytest

from wikigraph.paths import LATEST, BuildPaths, InvalidDate, check_date, dump_filename


def test_dump_filename_matches_wikimedia_convention() -> None:
    assert dump_filename("enwiki", "20261001", "pagelinks") == "enwiki-20261001-pagelinks.sql.gz"


def test_unknown_table_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown dump table"):
        dump_filename("enwiki", "20261001", "revision")


def test_layout(tmp_path: Path) -> None:
    paths = BuildPaths.of(tmp_path, "simplewiki", "20260101")
    build = tmp_path / "simplewiki" / "20260101"
    assert paths.base == build
    assert paths.raw == build / "raw"
    assert paths.work == build / "work"
    assert paths.dumpstatus == build / "raw" / "dumpstatus.json"
    assert paths.stats_file == build / "work" / "stats.jsonl"
    assert paths.raw_file("page") == build / "raw" / "simplewiki-20260101-page.sql.gz"
    assert paths.work_file("pages.parquet") == build / "work" / "pages.parquet"


def test_accepts_a_string_root() -> None:
    assert BuildPaths.of("data", "simplewiki", "20260101").root == Path("data")


def test_ensure_is_idempotent(tmp_path: Path) -> None:
    paths = BuildPaths.of(tmp_path, "simplewiki", "20260101")
    paths.ensure()
    paths.ensure()
    assert paths.raw.is_dir()
    assert paths.work.is_dir()


@pytest.mark.parametrize("date", ["2026-01-01", "202601", "", "2026010a", "202601011", " 20260101"])
def test_malformed_dates_are_rejected(tmp_path: Path, date: str) -> None:
    with pytest.raises(InvalidDate, match="YYYYMMDD"):
        BuildPaths.of(tmp_path, "simplewiki", date)


def test_latest_is_rejected_with_an_actionable_message() -> None:
    # 'latest' is a stage-1 concept; everything downstream needs a pinned date.
    with pytest.raises(InvalidDate, match="wikigraph fetch"):
        check_date(LATEST)


def test_check_date_returns_a_good_date() -> None:
    assert check_date("20261001") == "20261001"


def test_empty_wiki_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        BuildPaths.of(tmp_path, "", "20260101")


def test_is_hashable_and_comparable(tmp_path: Path) -> None:
    one = BuildPaths.of(tmp_path, "simplewiki", "20260101")
    two = BuildPaths.of(tmp_path, "simplewiki", "20260101")
    assert one == two
    assert len({one, two}) == 1
