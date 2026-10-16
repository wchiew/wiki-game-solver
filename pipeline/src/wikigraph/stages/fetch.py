"""Stage 1: acquire the four dump files from one dump run, and verify them.

``dumpstatus.json`` is the source of truth rather than ``sha1sums.txt``: it
carries each file's ``status``, ``size``, ``sha1`` and ``url`` together, so one
request gives both the completeness check and the checksums. Files are located
by *filename* inside the jobs map, which avoids hardcoding job names like
``pagelinkstable``.

The hard rule is that all four files must come from the same run and report
``status: "done"``. A half-written dump is the worst possible input, because it
fails several stages later and looks like a logic bug.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from ..paths import LATEST, TABLES, BuildPaths, check_date, dump_filename
from ..stats import stage_stats

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://dumps.wikimedia.org"

_UA_BASE = "wiki-game-solver/0.1 (Wikipedia pathfinder; offline dump build)"
_DATE_LINK_RE = re.compile(r'href="(\d{8})/?"')
_BLOCK = 1 << 20


class FetchError(Exception):
    """The dump run is unusable, or a downloaded file did not verify."""


def user_agent(contact: str | None = None) -> str:
    """Build a User-Agent. Wikimedia asks that it identify the tool and a contact."""
    return f"{_UA_BASE} <{contact}>" if contact else _UA_BASE


class Fetcher(Protocol):
    """The HTTP surface stage 1 needs. Tests substitute a fake."""

    def get_json(self, url: str) -> Any: ...

    def get_text(self, url: str) -> str: ...

    def download(self, url: str, dest: Path, *, resume_from: int = 0) -> None: ...


class HttpxFetcher:
    """A real HTTP client: sequential, redirect-following, resumable."""

    def __init__(self, contact: str | None = None, timeout: float = 60.0) -> None:
        self._client = httpx.Client(
            headers={"User-Agent": user_agent(contact)},
            timeout=timeout,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpxFetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def get_json(self, url: str) -> Any:
        response = self._client.get(url)
        response.raise_for_status()
        return response.json()

    def get_text(self, url: str) -> str:
        response = self._client.get(url)
        response.raise_for_status()
        return response.text

    def download(self, url: str, dest: Path, *, resume_from: int = 0) -> None:
        headers = {"Range": f"bytes={resume_from}-"} if resume_from else {}
        with self._client.stream("GET", url, headers=headers) as response:
            response.raise_for_status()
            append = resume_from > 0 and response.status_code == 206
            if resume_from and not append:
                log.info("server ignored Range for %s; restarting the download", dest.name)
            with dest.open("ab" if append else "wb") as fh:
                for chunk in response.iter_bytes(_BLOCK):
                    fh.write(chunk)


@dataclass(frozen=True)
class RemoteFile:
    """One dump file as ``dumpstatus.json`` describes it."""

    table: str
    filename: str
    url: str
    size: int | None
    sha1: str | None
    job: str


def _absolute_url(base_url: str, entry_url: str | None, wiki: str, date: str, name: str) -> str:
    if entry_url:
        if entry_url.startswith(("http://", "https://")):
            return entry_url
        return f"{base_url}/{entry_url.lstrip('/')}"
    return f"{base_url}/{wiki}/{date}/{name}"


def remote_files(status: Any, wiki: str, date: str, base_url: str) -> dict[str, RemoteFile]:
    """Locate the four wanted files in a ``dumpstatus.json`` payload.

    Raises :class:`FetchError` if any is absent or its job is not finished.
    """
    jobs = (status or {}).get("jobs") or {}
    found: dict[str, RemoteFile] = {}
    for table in TABLES:
        name = dump_filename(wiki, date, table)
        hit = None
        for job_name, job in jobs.items():
            entry = ((job or {}).get("files") or {}).get(name)
            if entry is not None:
                hit = (job_name, job or {}, entry)
                break
        if hit is None:
            raise FetchError(
                f"{name} is not listed in dumpstatus.json for {wiki} {date}; "
                "this dump run may not produce it"
            )
        job_name, job, entry = hit
        job_status = job.get("status")
        if job_status != "done":
            raise FetchError(
                f"dump job {job_name!r}, which produces {name}, has status {job_status!r} "
                "rather than 'done'; wait for the run to finish"
            )
        found[table] = RemoteFile(
            table=table,
            filename=name,
            url=_absolute_url(base_url, entry.get("url"), wiki, date, name),
            size=entry.get("size"),
            sha1=entry.get("sha1"),
            job=job_name,
        )
    return found


def resolve_date(fetcher: Fetcher, base_url: str, wiki: str, date: str) -> str:
    """Turn ``latest`` into the newest dump run that actually has all four files.

    Wikimedia's ``/latest/`` directory uses ``-latest-`` filenames and so hides
    the date, but the date *is* the graph version. So walk the dated directories
    newest-first and take the first complete one.
    """
    if date != LATEST:
        return check_date(date)

    index = fetcher.get_text(f"{base_url}/{wiki}/")
    candidates = sorted({m.group(1) for m in _DATE_LINK_RE.finditer(index)}, reverse=True)
    if not candidates:
        raise FetchError(f"no dated dump directories found at {base_url}/{wiki}/")

    for candidate in candidates:
        try:
            status = fetcher.get_json(f"{base_url}/{wiki}/{candidate}/dumpstatus.json")
            remote_files(status, wiki, candidate, base_url)
        except (FetchError, httpx.HTTPError, ValueError) as exc:
            log.info("dump run %s is not usable (%s); trying the previous one", candidate, exc)
            continue
        return candidate

    raise FetchError(
        f"none of the {len(candidates)} dump runs for {wiki} have all of {TABLES} finished"
    )


def _sha1_of(path: Path) -> str:
    digest = hashlib.sha1(usedforsecurity=False)  # a checksum, not a security boundary
    with path.open("rb") as fh:
        while chunk := fh.read(_BLOCK):
            digest.update(chunk)
    return digest.hexdigest()


def _sidecar(dest: Path, suffix: str) -> Path:
    return dest.parent / (dest.name + suffix)


def _acquire(fetcher: Fetcher, remote: RemoteFile, dest: Path, *, force: bool) -> tuple[str, int]:
    """Make *dest* exist and verify. Returns (outcome, bytes on disk).

    ``dest`` only ever appears once its checksum has passed, so a file that is
    present with the right size and a matching marker needs no work at all --
    which matters because re-hashing 7 GB on every pipeline re-run is minutes
    of pure waste.
    """
    marker = _sidecar(dest, ".sha1")
    part = _sidecar(dest, ".part")

    if force:
        for stale in (dest, marker, part):
            stale.unlink(missing_ok=True)

    if dest.exists():
        if remote.size is not None and dest.stat().st_size != remote.size:
            # `dest` only ever appears after its checksum passed, so a wrong
            # size means it is not a trustworthy prefix. Discard it rather than
            # resuming on top of it -- only an explicit `.part` is resumable.
            log.warning(
                "%s is %d bytes but the dump run says %d; discarding it",
                dest.name,
                dest.stat().st_size,
                remote.size,
            )
            marker.unlink(missing_ok=True)
            dest.unlink()
        else:
            if remote.sha1 and marker.exists() and marker.read_text().strip() == remote.sha1:
                return "cached", dest.stat().st_size
            digest = _sha1_of(dest)
            if remote.sha1 and digest != remote.sha1:
                raise FetchError(
                    f"{remote.filename} is already present but its checksum is wrong "
                    f"(expected {remote.sha1}, got {digest}); delete it or pass --force"
                )
            marker.write_text(digest + "\n")
            return "verified", dest.stat().st_size

    resume_from = part.stat().st_size if part.exists() else 0
    if resume_from:
        log.info("resuming %s at %d bytes", remote.filename, resume_from)
    fetcher.download(remote.url, part, resume_from=resume_from)

    size = part.stat().st_size
    if remote.size is not None and size != remote.size:
        raise FetchError(
            f"{remote.filename} downloaded as {size} bytes but the dump run says {remote.size}"
        )
    digest = _sha1_of(part)
    if remote.sha1 and digest != remote.sha1:
        raise FetchError(
            f"checksum mismatch for {remote.filename}: expected {remote.sha1}, got {digest}"
        )
    part.replace(dest)
    marker.write_text(digest + "\n")
    return "downloaded", size


def run(
    *,
    root: Path | str,
    wiki: str,
    date: str,
    fetcher: Fetcher,
    base_url: str = DEFAULT_BASE_URL,
    force: bool = False,
) -> tuple[BuildPaths, dict[str, Path]]:
    """Fetch and verify the four dumps. Returns the pinned paths and each file."""
    base_url = base_url.rstrip("/")
    resolved = resolve_date(fetcher, base_url, wiki, date)
    if resolved != date:
        log.info("resolved --date %s to %s", date, resolved)

    paths = BuildPaths.of(root, wiki, resolved).ensure()
    status = fetcher.get_json(f"{base_url}/{wiki}/{resolved}/dumpstatus.json")
    remotes = remote_files(status, wiki, resolved, base_url)
    paths.dumpstatus.write_text(json.dumps(status, indent=2, sort_keys=True), encoding="utf-8")

    files: dict[str, Path] = {}
    with stage_stats(paths, "fetch") as stats:
        outcomes: dict[str, str] = {}
        total = 0
        for table in TABLES:
            remote = remotes[table]
            dest = paths.raw_file(table)
            outcome, size = _acquire(fetcher, remote, dest, force=force)
            log.info("%s: %s (%.1f MiB)", remote.filename, outcome, size / 1048576)
            outcomes[table] = outcome
            total += size
            files[table] = dest
        stats.rows_out = len(files)
        stats.extra.update(date=resolved, outcomes=outcomes, bytes=total)
    return paths, files
