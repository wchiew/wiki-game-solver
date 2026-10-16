"""Command line interface: one subcommand per pipeline stage.

All ten stages are registered so that ``wikigraph --help`` documents the whole
pipeline; the eight that are not built yet say so and exit non-zero. See
``docs/pipeline.md`` for the stage specs and ``docs/roadmap.md`` for the order
they are being built in.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from .paths import LATEST, BuildPaths, InvalidDate
from .stages import fetch as fetch_stage
from .stages import pages as pages_stage

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help=(
        "Turn Wikimedia SQL dumps into a compact link graph. "
        "Stages are file-to-file and re-runnable; develop against --wiki simplewiki."
    ),
)

WikiOpt = Annotated[str, typer.Option("--wiki", help="Wiki database name, e.g. enwiki.")]
DateOpt = Annotated[
    str,
    typer.Option("--date", help="Dump date as YYYYMMDD. 'latest' is resolved by `fetch` only."),
]
RootOpt = Annotated[
    Path | None,
    typer.Option("--root", help="Where builds live. Default $WIKIGRAPH_DATA_ROOT or ./data."),
]
VerboseOpt = Annotated[bool, typer.Option("--verbose", "-v", help="Debug-level logging.")]

DEFAULT_WIKI = "simplewiki"


def _default_root() -> Path:
    return Path(os.environ.get("WIKIGRAPH_DATA_ROOT", "data"))


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )


def _build_paths(root: Path | None, wiki: str, date: str) -> BuildPaths:
    try:
        return BuildPaths.of(root or _default_root(), wiki, date)
    except InvalidDate as exc:
        raise typer.BadParameter(str(exc), param_hint="--date") from exc


@app.command()
def fetch(
    wiki: WikiOpt = DEFAULT_WIKI,
    date: DateOpt = LATEST,
    root: RootOpt = None,
    verbose: VerboseOpt = False,
    base_url: Annotated[
        str, typer.Option("--base-url", help="Dump host; point at a mirror to spare the primary.")
    ] = fetch_stage.DEFAULT_BASE_URL,
    contact: Annotated[
        str | None,
        typer.Option("--contact", help="Contact for the User-Agent. Or set $WIKIGRAPH_CONTACT."),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Re-download even if a verified copy is present.")
    ] = False,
) -> None:
    """Stage 1: download and verify the four dumps from one dump run."""
    _setup_logging(verbose)
    contact = contact or os.environ.get("WIKIGRAPH_CONTACT")
    if not contact:
        typer.secho(
            "note: no --contact or $WIKIGRAPH_CONTACT set. Wikimedia asks that automated "
            "clients identify a contact; set one before any sizeable download.",
            fg=typer.colors.YELLOW,
            err=True,
        )

    with fetch_stage.HttpxFetcher(contact=contact) as fetcher:
        paths, files = fetch_stage.run(
            root=root or _default_root(),
            wiki=wiki,
            date=date,
            fetcher=fetcher,
            base_url=base_url,
            force=force,
        )

    typer.echo(f"dump date {paths.date}")
    for table, path in files.items():
        typer.echo(f"  {table:<10} {path}")
    typer.echo(f"\nnext: wikigraph pages --wiki {wiki} --date {paths.date}")


@app.command()
def pages(
    wiki: WikiOpt = DEFAULT_WIKI,
    date: DateOpt = LATEST,
    root: RootOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Stage 2: keep namespace 0 and write work/pages.parquet."""
    _setup_logging(verbose)
    out = pages_stage.run(_build_paths(root, wiki, date))
    typer.echo(out)


@app.command()
def build(
    wiki: WikiOpt = DEFAULT_WIKI,
    date: DateOpt = LATEST,
    root: RootOpt = None,
    verbose: VerboseOpt = False,
    contact: Annotated[str | None, typer.Option("--contact")] = None,
) -> None:
    """Run every implemented stage in order."""
    _setup_logging(verbose)
    contact = contact or os.environ.get("WIKIGRAPH_CONTACT")
    with fetch_stage.HttpxFetcher(contact=contact) as fetcher:
        paths, _ = fetch_stage.run(
            root=root or _default_root(), wiki=wiki, date=date, fetcher=fetcher
        )
    pages_stage.run(paths)
    typer.secho(
        f"stages 1-2 done for {paths.wiki} {paths.date}; stages 3-10 are not built yet "
        "(see docs/roadmap.md)",
        fg=typer.colors.YELLOW,
    )


# --------------------------------------------------------------------------- #
# Stages not built yet. Registered so `--help` documents the whole pipeline.
# --------------------------------------------------------------------------- #

_PENDING: dict[str, str] = {
    "redirects": "stage 3: resolve redirect chains to canonical page ids",
    "dense-ids": "stage 4: number non-redirect articles 0..N-1",
    "link-targets": "stage 5: resolve linktarget rows to dense ids",
    "edges": "stage 6: stream pagelinks into deduped edge chunks",
    "csr": "stage 7: sort, dedupe, and build the forward and reverse CSRs",
    "lookups": "stage 8: titles blob and the sorted normalized-title index",
    "validate": "stage 9: structural invariants, golden fixtures, drift",
    "publish": "stage 10: write manifest.json and flip graphs/current",
}


def _make_pending(name: str, summary: str):
    def command() -> NoReturn:
        typer.secho(
            f"`wikigraph {name}` is not implemented yet ({summary}). See docs/roadmap.md.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        raise typer.Exit(code=2)

    command.__name__ = name.replace("-", "_")
    command.__doc__ = f"{summary[0].upper()}{summary[1:]}. Not implemented yet."
    return command


for _name, _summary in _PENDING.items():
    app.command(name=_name)(_make_pending(_name, _summary))


if __name__ == "__main__":
    app()
