"""Tests for stage 2.

The interesting assertions are the ones about *form*: titles must come through
with underscores and punctuation intact, because stage 3 joins
``redirect.rd_title`` against this column in exactly the dump's own spelling.
"""

from __future__ import annotations

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from helpers import page_row, write_page_dump
from wikigraph.paths import BuildPaths
from wikigraph.stages.pages import PAGES_SCHEMA, PagesError, run
from wikigraph.stats import read_stats

#: Namespace-0 page_ids in ``fixtures/tiny/page.sql``, in dump order.
NS0_IDS = [10, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 27, 28, 29]


def test_keeps_only_namespace_zero(tiny_build: BuildPaths) -> None:
    data = pq.read_table(run(tiny_build))
    assert data.column("page_id").to_pylist() == NS0_IDS


def test_titles_keep_the_raw_dump_form(tiny_build: BuildPaths) -> None:
    titles = set(pq.read_table(run(tiny_build)).column("title").to_pylist())
    assert titles == {
        "AccessibleComputing",
        "Anarchism",
        "Alien_(film)",
        "Smith,_John",
        "It's_a_Wonderful_Life",
        'Say_"Hello"',
        "Back\\slash",
        "Trailing\\",
        "Line\nBreak",
        "Fifty\\%_Off",
        "Doubled'Quote",
        "Zürich",
        "日本",
        "Semicolon;_Test",
        "Null_And_Negative",
        "Hex_Touched",
    }


def test_underscores_are_not_converted(tiny_build: BuildPaths) -> None:
    # Normalization is stage 8's job. Doing it here would break stage 3's join.
    titles = pq.read_table(run(tiny_build)).column("title").to_pylist()
    assert "Alien_(film)" in titles
    assert "Alien (film)" not in titles


def test_redirect_flag(tiny_build: BuildPaths) -> None:
    data = pq.read_table(run(tiny_build))
    flags = dict(
        zip(
            data.column("page_id").to_pylist(),
            data.column("is_redirect").to_pylist(),
            strict=True,
        )
    )
    assert flags[10] is True
    assert flags[12] is False
    assert sum(flags.values()) == 1


def test_schema_is_the_declared_one(tiny_build: BuildPaths) -> None:
    schema = pq.read_table(run(tiny_build)).schema
    assert schema.field("page_id").type == pa.uint32()
    assert schema.field("title").type == pa.string()
    assert schema.field("is_redirect").type == pa.bool_()
    assert schema.names == PAGES_SCHEMA.names


def test_stats_record(tiny_build: BuildPaths) -> None:
    run(tiny_build)
    records = [r for r in read_stats(tiny_build) if r["stage"] == "pages"]
    assert len(records) == 1
    record = records[0]
    assert record["ok"] is True
    assert record["rows_in"] == 19
    assert record["rows_out"] == 16
    assert record["dropped"] == {"namespace": 3}
    assert record["redirects"] == 1
    assert record["articles"] == 15


@pytest.mark.parametrize("batch_size", [1, 2, 5, 15, 16, 17, 10_000])
def test_batch_size_does_not_change_the_content(tiny_build: BuildPaths, batch_size: int) -> None:
    data = pq.read_table(run(tiny_build, batch_size=batch_size))
    assert data.column("page_id").to_pylist() == NS0_IDS


def test_rerun_produces_the_same_content(tiny_build: BuildPaths) -> None:
    first = pq.read_table(run(tiny_build))
    second = pq.read_table(run(tiny_build))
    assert first.equals(second)


def test_no_temporary_file_is_left_behind(tiny_build: BuildPaths) -> None:
    run(tiny_build)
    assert list(tiny_build.work.glob("*.tmp")) == []


def test_missing_source_says_what_to_do(empty_build: BuildPaths) -> None:
    with pytest.raises(PagesError, match="run `wikigraph fetch`"):
        run(empty_build)


def test_invalid_utf8_title_is_dropped_and_counted(empty_build: BuildPaths) -> None:
    write_page_dump(
        empty_build.raw_file("page"),
        page_row(1, 0, b"Good"),
        page_row(2, 0, b"Bad\xff\xfeTitle"),
        page_row(3, 0, b"Also_Good"),
    )
    data = pq.read_table(run(empty_build))
    assert data.column("title").to_pylist() == ["Good", "Also_Good"]
    assert read_stats(empty_build)[-1]["dropped"] == {"decode_error": 1}


def test_duplicate_page_id_is_an_error(empty_build: BuildPaths) -> None:
    write_page_dump(
        empty_build.raw_file("page"),
        page_row(1, 0, b"First"),
        page_row(1, 0, b"Second"),
    )
    with pytest.raises(PagesError, match="page_id is not unique"):
        run(empty_build)


def test_duplicate_title_is_an_error(empty_build: BuildPaths) -> None:
    write_page_dump(
        empty_build.raw_file("page"),
        page_row(1, 0, b"Same"),
        page_row(2, 0, b"Same"),
    )
    with pytest.raises(PagesError, match="title is not unique"):
        run(empty_build)


def test_a_failed_check_publishes_nothing(empty_build: BuildPaths) -> None:
    """Verification runs on the temp file, so the real path stays untouched."""
    write_page_dump(
        empty_build.raw_file("page"),
        page_row(1, 0, b"Same"),
        page_row(2, 0, b"Same"),
    )
    with pytest.raises(PagesError):
        run(empty_build)
    assert not empty_build.work_file("pages.parquet").exists()
    assert read_stats(empty_build)[-1]["ok"] is False


def test_all_rows_filtered_out_is_not_an_error(empty_build: BuildPaths) -> None:
    write_page_dump(
        empty_build.raw_file("page"),
        page_row(1, 14, b"Category_only"),
    )
    data = pq.read_table(run(empty_build))
    assert data.num_rows == 0
    assert read_stats(empty_build)[-1]["dropped"] == {"namespace": 1}
