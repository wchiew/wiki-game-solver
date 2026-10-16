"""Stage 2: the article inventory.

Reads the ``page`` dump, keeps namespace 0, and writes ``work/pages.parquet``.

``title`` holds the **raw dump form: underscores intact, no namespace prefix.**
Normalization belongs to stage 8, and stage 3 joins ``redirect.rd_title``
against this column in exactly this form, so converting here would silently
break that join.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from ..paths import BuildPaths
from ..sqldump import open_dump
from ..stats import StageStats, stage_stats

log = logging.getLogger(__name__)

PAGES_SCHEMA = pa.schema(
    [
        pa.field("page_id", pa.uint32(), nullable=False),
        pa.field("title", pa.string(), nullable=False),
        pa.field("is_redirect", pa.bool_(), nullable=False),
    ]
)

DEFAULT_BATCH = 500_000

#: How many bad titles to name in the log before going quiet.
_LOG_LIMIT = 20


class PagesError(Exception):
    """The page dump could not be turned into a usable inventory."""


def run(paths: BuildPaths, *, batch_size: int = DEFAULT_BATCH) -> Path:
    """Build ``work/pages.parquet``. Returns its path."""
    source = paths.raw_file("page")
    if not source.exists():
        raise PagesError(f"{source} is missing; run `wikigraph fetch` first")

    paths.work.mkdir(parents=True, exist_ok=True)
    out = paths.work_file("pages.parquet")
    tmp = out.parent / (out.name + ".tmp")

    with stage_stats(paths, "pages") as stats:
        _write(open_dump(source), tmp, stats, batch_size=batch_size)
        # Verify before publishing, so that neither a crash nor a failed check
        # can leave something at the real path that looks like a valid artifact.
        rows, redirects = _verify(tmp)
        tmp.replace(out)

        stats.rows_out = rows
        stats.extra.update(redirects=redirects, articles=rows - redirects)
        log.info(
            "pages: %d ns0 rows (%d articles, %d redirects), %d dropped",
            rows,
            rows - redirects,
            redirects,
            stats.total_dropped,
        )
    return out


def _write(reader, tmp: Path, stats: StageStats, *, batch_size: int) -> None:
    ids: list[int] = []
    titles: list[str] = []
    flags: list[bool] = []
    bad_titles = 0

    def flush(writer: pq.ParquetWriter) -> None:
        if not ids:
            return
        writer.write_table(
            pa.table({"page_id": ids, "title": titles, "is_redirect": flags}, schema=PAGES_SCHEMA)
        )
        ids.clear()
        titles.clear()
        flags.clear()

    with pq.ParquetWriter(tmp, PAGES_SCHEMA) as writer:
        for page_id, namespace, title, is_redirect in reader.rows(
            "page_id", "page_namespace", "page_title", "page_is_redirect"
        ):
            stats.rows_in += 1

            # A bytes compare, not int(): this runs once per row of the whole
            # page table, and mysqldump always writes a bare `0`.
            if namespace != b"0":
                stats.drop("namespace")
                continue
            if page_id is None or title is None:
                stats.drop("null_column")
                continue
            try:
                text = title.decode("utf-8")
            except UnicodeDecodeError:
                # Mojibake here would end up in the stage-8 title index, where a
                # mismatch with the Rust normalizer is a silent wrong answer
                # rather than a crash. Dropping is the safe direction.
                stats.drop("decode_error")
                bad_titles += 1
                if bad_titles <= _LOG_LIMIT:
                    log.warning("page_id=%s has a title that is not valid UTF-8; dropped", page_id)
                continue

            ids.append(int(page_id))
            titles.append(text)
            flags.append(is_redirect == b"1")
            if len(ids) >= batch_size:
                flush(writer)
        flush(writer)

    if bad_titles > _LOG_LIMIT:
        log.warning("%d titles in total were not valid UTF-8", bad_titles)


def _verify(parquet: Path) -> tuple[int, int]:
    """Check the uniqueness MediaWiki guarantees. Returns (rows, redirects).

    A violation means the parser read the wrong column, not that the data is
    odd -- MediaWiki enforces both of these, so they are a sharp test of the
    whole read path. DuckDB does the counting because it stays out-of-core at
    enwiki scale.
    """
    connection = duckdb.connect()
    try:
        rows, distinct_ids, distinct_titles, redirects = connection.execute(
            """
            SELECT count(*),
                   count(DISTINCT page_id),
                   count(DISTINCT title),
                   count(*) FILTER (WHERE is_redirect)
            FROM read_parquet(?)
            """,
            [str(parquet)],
        ).fetchone()
    finally:
        connection.close()

    if distinct_ids != rows:
        raise PagesError(
            f"page_id is not unique: {rows} rows but {distinct_ids} distinct values. "
            "MediaWiki guarantees uniqueness, so the parser is reading the wrong column"
        )
    if distinct_titles != rows:
        raise PagesError(
            f"title is not unique: {rows} rows but {distinct_titles} distinct values. "
            "MediaWiki guarantees uniqueness within a namespace, so the parser is reading "
            "the wrong column"
        )
    return rows, redirects
