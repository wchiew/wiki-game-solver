"""Tests for stage 1.

Everything runs against a fake HTTP layer. The one real-network test is marked
``network`` and deselected by default, because a test suite that depends on
dumps.wikimedia.org being up is a test suite you stop trusting.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from wikigraph.paths import TABLES, dump_filename
from wikigraph.stages import fetch as fetch_stage
from wikigraph.stages.fetch import FetchError

BASE = "https://dumps.example.org"
WIKI = "simplewiki"
DATE = "20260101"

#: Which dump job produces which table, as dumpstatus.json reports it. The code
#: must not depend on these names -- it looks files up by filename -- so the
#: tests deliberately use plausible-but-incidental job names.
JOBS = {
    "page": "pagetable",
    "pagelinks": "pagelinkstable",
    "linktarget": "linktargettable",
    "redirect": "redirecttable",
}


def blobs(date: str = DATE) -> dict[str, bytes]:
    return {table: (f"-- {table} {date}\n" * 3).encode() for table in TABLES}


def make_status(
    contents: dict[str, bytes],
    *,
    wiki: str = WIKI,
    date: str = DATE,
    statuses: dict[str, str] | None = None,
    with_sha1: bool = True,
    urls: dict[str, str] | None = None,
) -> dict[str, Any]:
    statuses = statuses or {}
    urls = urls or {}
    jobs: dict[str, Any] = {}
    for table, blob in contents.items():
        name = dump_filename(wiki, date, table)
        entry: dict[str, Any] = {
            "size": len(blob),
            "url": urls.get(table, f"/{wiki}/{date}/{name}"),
        }
        if with_sha1:
            entry["sha1"] = hashlib.sha1(blob, usedforsecurity=False).hexdigest()
        jobs[JOBS[table]] = {"status": statuses.get(table, "done"), "files": {name: entry}}
    return {"version": "0.8", "jobs": jobs}


class FakeFetcher:
    """Serves whatever it was handed; records what was asked for."""

    def __init__(
        self,
        *,
        json_at: dict[str, Any] | None = None,
        text_at: dict[str, str] | None = None,
        blob_at: dict[str, bytes] | None = None,
    ) -> None:
        self.json_at = json_at or {}
        self.text_at = text_at or {}
        self.blob_at = blob_at or {}
        self.downloads: list[tuple[str, int]] = []

    def get_json(self, url: str) -> Any:
        if url not in self.json_at:
            raise httpx.HTTPError(f"404 for {url}")
        return self.json_at[url]

    def get_text(self, url: str) -> str:
        if url not in self.text_at:
            raise httpx.HTTPError(f"404 for {url}")
        return self.text_at[url]

    def download(self, url: str, dest: Path, *, resume_from: int = 0) -> None:
        self.downloads.append((url, resume_from))
        data = self.blob_at[url]
        with dest.open("ab" if resume_from else "wb") as fh:
            fh.write(data[resume_from:])


def wire(contents: dict[str, bytes], **kwargs: Any) -> FakeFetcher:
    """A fetcher serving one complete dump run at a pinned date."""
    status = make_status(contents, **kwargs)
    return FakeFetcher(
        json_at={f"{BASE}/{WIKI}/{DATE}/dumpstatus.json": status},
        blob_at={
            f"{BASE}/{WIKI}/{DATE}/{dump_filename(WIKI, DATE, table)}": blob
            for table, blob in contents.items()
        },
    )


def go(tmp_path: Path, fetcher: FakeFetcher, *, date: str = DATE, force: bool = False) -> Any:
    return fetch_stage.run(
        root=tmp_path, wiki=WIKI, date=date, fetcher=fetcher, base_url=BASE, force=force
    )


def url_for(table: str, *, date: str = DATE) -> str:
    return f"{BASE}/{WIKI}/{date}/{dump_filename(WIKI, date, table)}"


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_downloads_and_verifies_all_four(tmp_path: Path) -> None:
    contents = blobs()
    fetcher = wire(contents)
    paths, files = go(tmp_path, fetcher)

    assert paths.date == DATE
    assert set(files) == set(TABLES)
    for table in TABLES:
        assert files[table].read_bytes() == contents[table]
    assert len(fetcher.downloads) == 4
    assert all(resume == 0 for _, resume in fetcher.downloads)


def test_dumpstatus_is_recorded_for_provenance(tmp_path: Path) -> None:
    fetcher = wire(blobs())
    paths, _ = go(tmp_path, fetcher)
    assert (
        json.loads(paths.dumpstatus.read_text(encoding="utf-8"))
        == fetcher.json_at[f"{BASE}/{WIKI}/{DATE}/dumpstatus.json"]
    )


def test_second_run_re_uses_the_verified_copies(tmp_path: Path) -> None:
    fetcher = wire(blobs())
    go(tmp_path, fetcher)
    assert len(fetcher.downloads) == 4
    go(tmp_path, fetcher)
    # Re-hashing gigabytes on every pipeline re-run is pure waste, so a marker
    # file records that this exact checksum already passed.
    assert len(fetcher.downloads) == 4


def test_force_downloads_again(tmp_path: Path) -> None:
    fetcher = wire(blobs())
    go(tmp_path, fetcher)
    go(tmp_path, fetcher, force=True)
    assert len(fetcher.downloads) == 8


def test_a_status_without_checksums_still_works(tmp_path: Path) -> None:
    contents = blobs()
    fetcher = wire(contents, with_sha1=False)
    _, files = go(tmp_path, fetcher)
    assert files["page"].read_bytes() == contents["page"]


@pytest.mark.parametrize(
    ("entry_url", "expected"),
    [
        (f"/{WIKI}/{DATE}/custom.sql.gz", f"{BASE}/{WIKI}/{DATE}/custom.sql.gz"),
        ("https://mirror.example/custom.sql.gz", "https://mirror.example/custom.sql.gz"),
    ],
)
def test_url_forms_from_dumpstatus(entry_url: str, expected: str) -> None:
    status = make_status(blobs(), urls={"page": entry_url})
    remotes = fetch_stage.remote_files(status, WIKI, DATE, BASE)
    assert remotes["page"].url == expected


def test_url_is_constructed_when_dumpstatus_omits_it() -> None:
    status = make_status(blobs())
    for job in status["jobs"].values():
        for entry in job["files"].values():
            entry.pop("url")
    remotes = fetch_stage.remote_files(status, WIKI, DATE, BASE)
    assert remotes["page"].url == url_for("page")


# --------------------------------------------------------------------------- #
# Refusing bad input
# --------------------------------------------------------------------------- #


def test_an_unfinished_job_is_refused(tmp_path: Path) -> None:
    fetcher = wire(blobs(), statuses={"pagelinks": "waiting"})
    with pytest.raises(FetchError, match="rather than 'done'"):
        go(tmp_path, fetcher)
    assert fetcher.downloads == []


def test_a_missing_table_is_refused(tmp_path: Path) -> None:
    contents = blobs()
    del contents["linktarget"]
    fetcher = wire(contents)
    with pytest.raises(FetchError, match="not listed in dumpstatus"):
        go(tmp_path, fetcher)


def test_checksum_mismatch_publishes_nothing(tmp_path: Path) -> None:
    contents = blobs()
    fetcher = wire(contents)
    # Same length, different bytes: the size check passes, the digest does not.
    fetcher.blob_at[url_for("page")] = b"X" * len(contents["page"])
    with pytest.raises(FetchError, match="checksum mismatch"):
        go(tmp_path, fetcher)
    assert not (tmp_path / WIKI / DATE / "raw" / dump_filename(WIKI, DATE, "page")).exists()


def test_size_mismatch_publishes_nothing(tmp_path: Path) -> None:
    contents = blobs()
    fetcher = wire(contents)
    fetcher.blob_at[url_for("page")] = contents["page"] + b"unexpected tail"
    with pytest.raises(FetchError, match="downloaded as"):
        go(tmp_path, fetcher)
    assert not (tmp_path / WIKI / DATE / "raw" / dump_filename(WIKI, DATE, "page")).exists()


def test_an_existing_file_with_a_bad_checksum_is_reported(tmp_path: Path) -> None:
    contents = blobs()
    fetcher = wire(contents)
    raw = tmp_path / WIKI / DATE / "raw"
    raw.mkdir(parents=True)
    # Right size, wrong bytes, and no marker to vouch for it.
    (raw / dump_filename(WIKI, DATE, "page")).write_bytes(b"X" * len(contents["page"]))
    with pytest.raises(FetchError, match="already present but its checksum is wrong"):
        go(tmp_path, fetcher)


# --------------------------------------------------------------------------- #
# Resume
# --------------------------------------------------------------------------- #


def test_a_partial_download_is_resumed(tmp_path: Path) -> None:
    contents = blobs()
    fetcher = wire(contents)
    raw = tmp_path / WIKI / DATE / "raw"
    raw.mkdir(parents=True)
    (raw / (dump_filename(WIKI, DATE, "page") + ".part")).write_bytes(contents["page"][:7])

    _, files = go(tmp_path, fetcher)
    assert files["page"].read_bytes() == contents["page"]
    assert (url_for("page"), 7) in fetcher.downloads


def test_a_wrong_size_published_file_is_discarded_not_resumed(tmp_path: Path) -> None:
    """A published file is post-verification, so a wrong size means untrustworthy.

    Resuming on top of it would splice a corrupt prefix onto good bytes and only
    fail at the checksum, after wasting the whole download.
    """
    contents = blobs()
    fetcher = wire(contents)
    raw = tmp_path / WIKI / DATE / "raw"
    raw.mkdir(parents=True)
    (raw / dump_filename(WIKI, DATE, "page")).write_bytes(b"truncated")

    _, files = go(tmp_path, fetcher)
    assert files["page"].read_bytes() == contents["page"]
    assert (url_for("page"), 0) in fetcher.downloads


# --------------------------------------------------------------------------- #
# Resolving `latest`
# --------------------------------------------------------------------------- #


def test_latest_picks_the_newest_complete_run(tmp_path: Path) -> None:
    old, new = "20251201", "20260101"
    old_contents, new_contents = blobs(old), blobs(new)
    index = f'<a href="{old}/">{old}/</a><a href="{new}/">{new}/</a><a href="latest/">latest/</a>'
    fetcher = FakeFetcher(
        text_at={f"{BASE}/{WIKI}/": index},
        json_at={
            f"{BASE}/{WIKI}/{old}/dumpstatus.json": make_status(old_contents, date=old),
            f"{BASE}/{WIKI}/{new}/dumpstatus.json": make_status(
                new_contents, date=new, statuses={"pagelinks": "waiting"}
            ),
        },
        blob_at={url_for(t, date=old): b for t, b in old_contents.items()},
    )

    paths, files = fetch_stage.run(
        root=tmp_path, wiki=WIKI, date="latest", fetcher=fetcher, base_url=BASE
    )
    assert paths.date == old
    assert files["page"].read_bytes() == old_contents["page"]


def test_latest_skips_a_run_with_no_dumpstatus(tmp_path: Path) -> None:
    old, new = "20251201", "20260101"
    old_contents = blobs(old)
    index = f'<a href="{old}/">{old}/</a><a href="{new}/">{new}/</a>'
    fetcher = FakeFetcher(
        text_at={f"{BASE}/{WIKI}/": index},
        json_at={f"{BASE}/{WIKI}/{old}/dumpstatus.json": make_status(old_contents, date=old)},
        blob_at={url_for(t, date=old): b for t, b in old_contents.items()},
    )
    paths, _ = fetch_stage.run(
        root=tmp_path, wiki=WIKI, date="latest", fetcher=fetcher, base_url=BASE
    )
    assert paths.date == old


def test_latest_with_no_dated_directories(tmp_path: Path) -> None:
    fetcher = FakeFetcher(text_at={f"{BASE}/{WIKI}/": "<html>nothing here</html>"})
    with pytest.raises(FetchError, match="no dated dump directories"):
        fetch_stage.run(root=tmp_path, wiki=WIKI, date="latest", fetcher=fetcher, base_url=BASE)


def test_latest_with_no_complete_run(tmp_path: Path) -> None:
    date = "20260101"
    fetcher = FakeFetcher(
        text_at={f"{BASE}/{WIKI}/": f'<a href="{date}/">{date}/</a>'},
        json_at={
            f"{BASE}/{WIKI}/{date}/dumpstatus.json": make_status(
                blobs(date), date=date, statuses={"page": "waiting"}
            )
        },
    )
    with pytest.raises(FetchError, match="have all of"):
        fetch_stage.run(root=tmp_path, wiki=WIKI, date="latest", fetcher=fetcher, base_url=BASE)


def test_a_pinned_date_needs_no_directory_listing(tmp_path: Path) -> None:
    fetcher = wire(blobs())  # no text_at at all
    paths, _ = go(tmp_path, fetcher)
    assert paths.date == DATE


# --------------------------------------------------------------------------- #
# Misc
# --------------------------------------------------------------------------- #


def test_stats_record(tmp_path: Path) -> None:
    from wikigraph.stats import read_stats

    fetcher = wire(blobs())
    paths, _ = go(tmp_path, fetcher)
    record = read_stats(paths)[-1]
    assert record["stage"] == "fetch"
    assert record["ok"] is True
    assert record["rows_out"] == 4
    assert record["date"] == DATE
    assert set(record["outcomes"]) == set(TABLES)
    assert record["bytes"] > 0


def test_user_agent_identifies_the_tool_and_contact() -> None:
    assert "wiki-game-solver" in fetch_stage.user_agent(None)
    assert "me@example.org" in fetch_stage.user_agent("me@example.org")


@pytest.mark.network
def test_real_dump_run_resolves() -> None:
    """Smoke test against the real dump host. Deselected unless -m network."""
    with fetch_stage.HttpxFetcher(contact="wiki-game-solver test suite") as fetcher:
        date = fetch_stage.resolve_date(
            fetcher, fetch_stage.DEFAULT_BASE_URL, "simplewiki", "latest"
        )
    assert len(date) == 8 and date.isdigit()
