from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from helpers import TINY
from wikigraph.paths import BuildPaths


@pytest.fixture
def tiny_page_dump(tmp_path: Path) -> Path:
    """The committed ``page`` fixture, gzipped the way a real dump arrives."""
    dest = tmp_path / "page.sql.gz"
    dest.write_bytes(gzip.compress((TINY / "page.sql").read_bytes()))
    return dest


@pytest.fixture
def tiny_build(tmp_path: Path) -> BuildPaths:
    """A build directory with the tiny ``page`` dump already in place."""
    paths = BuildPaths.of(tmp_path / "data", "simplewiki", "20260101").ensure()
    paths.raw_file("page").write_bytes(gzip.compress((TINY / "page.sql").read_bytes()))
    return paths


@pytest.fixture
def empty_build(tmp_path: Path) -> BuildPaths:
    """A build directory with no dumps in it."""
    return BuildPaths.of(tmp_path / "data", "simplewiki", "20260101").ensure()
