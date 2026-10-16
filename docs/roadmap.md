# Roadmap

Milestones in dependency order, each with an exit criterion that is checkable
rather than a matter of opinion. The sequencing principle: **prove the risky
part cheaply before building the expensive version of it.**

Status: nothing built yet. M0 is next.

---

## M0 · Scaffold

Make the repo runnable before there is anything to run.

- `pipeline/` — Python package `wikigraph`, `pyproject.toml`, Typer CLI with all
  ten stage subcommands stubbed, pytest + Hypothesis wired up
- `api/` — Rust crate `wiki-paths`, axum skeleton serving `/healthz`
- `bot/` — Python package `wikibot`, stub harness and policy protocol
- `fixtures/` — empty, with a README naming what goes in it
- Each subproject's own CLI is the task runner — no `make`/`just` layer;
  `.gitignore` for `data/`, `graphs/`, `target/`
- CI: `ruff` + `pytest` + `cargo fmt --check` + `cargo clippy` + `cargo test`

**Exit:** lint and tests pass for all three subprojects on a clean clone.

> **Partly done.** The pipeline half of this landed with M1: `pipeline/` has its
> Typer CLI with all ten stages registered, `uv sync` → `pytest` → `ruff` green.
> Still outstanding: the `api/` and `bot/` skeletons, and CI.

---

## M1 · Pipeline on Simple English

The first real milestone, and the one that shakes out the dump format.

- [x] `sqldump.py` streaming tokenizer, including DDL parsing and MySQL escapes
- [x] `fixtures/tiny/page.sql` — a deliberately nasty hand-built miniature dump
- [x] Stage 1 `fetch` — `dumpstatus.json`-driven, resumable, checksum-verified
- [x] Stage 2 `pages` — ns0 inventory to Parquet
- [ ] Stages 3–8
- [ ] `fixtures/normalization.jsonl` with the Python side tested against it
- [ ] `fixtures/tiny/` dumps for the other three tables, with expected CSR

**Exit:** `wikigraph build --wiki simplewiki` produces artifacts that pass every
structural invariant, and a hand-verified path (checked against the live site)
comes out correct.

> **Progress.** The tokenizer is done and carries the chunk's real risk, so it
> got the most scrutiny: 132 tests pass, including a block-size sweep from 1
> byte to 1 MiB (identical output at every size, which is what actually
> exercises the mid-tuple resume path) and a Hypothesis round-trip over
> arbitrary byte strings re-encoded the way mysqldump writes them. Stages 1–2
> have not yet met a real dump.

**Why first:** the schema change around `pagelinks`/`linktarget` is the single
most likely thing to be misunderstood, and it is cheapest to discover on a wiki
that builds in minutes.

---

## M2 · Search prototype in Python

Prove the algorithm before committing it to Rust.

- Bidirectional layered BFS, parent DAG, counting DP, uniform sampling, `d+1`
  top-up — all in Python against the `simplewiki` CSR
- The full test battery from
  [`algorithms.md §9`](algorithms.md#9--testing), including the `N ≤ 12`
  brute-force comparison and the chi-square uniformity check

**Exit:** path sets and counts match brute force on random graphs; the sampler
passes the uniformity test; every `d+1` path sampled is verified simple.

**Why before Rust:** these are the two load-bearing correctness arguments in the
project. Getting them wrong in Python costs an afternoon; getting them wrong in
Rust costs a week and is harder to see. This prototype also becomes the
reference implementation that the Rust must agree with on committed fixtures.

---

## M3 · Pipeline on English Wikipedia

Scale up a pipeline that is already known to be correct.

- Streaming CSR build for the enwiki edge volume (no materializing `src` in RAM)
- Chunked, restartable stage 6
- Stage 9 validate gate: structural + golden fixtures + drift
- Stage 10 publish with `manifest.json` and the atomic pointer flip
- `u32` width guards actually exercised

**Exit:** `graphs/<date>/` published with a complete manifest, stage 9 green,
real counts recorded — and the ARCHITECTURE sizing estimates replaced with
measured numbers.

---

## M4 · The Rust API

- memmap loading with load-time validation; `arc-swap` hot reload
- `normalize()` in Rust, green against `fixtures/normalization.jsonl`
- Binary-search resolution over the index
- The search, ported from M2, with pooled epoch scratch arrays
- `GET /paths`, `POST /resolve`, `POST /next-hop`, `GET /suggest`, `/healthz`, `/graph`
- Budgets, the concurrency limit, the LRU cache
- Agreement tests: Rust and the M2 Python reference produce identical output on
  committed fixtures

**Exit:** p50 and p99 latency measured on a sampled workload against the real
enwiki graph, published next to the N1 target; a hot-swap performed with no
dropped requests.

---

## M5 · Harness and oracle bot

- Harness: rules config, game loop, scoring against the API as judge, run records
- MediaWiki client: polite, cached, record/replay
- Oracle policy with pool voting, pruning, and `/next-hop` re-planning

**Exit:** the oracle wins a set of hand-picked games; every run is reproducible
from the replay cache; oracle stretch is reported with its staleness-driven
excess over 1.0 visible.

---

## M6 · Fair policy and evaluation

- Embedding-similarity policy, visited set, hub heuristic
- Committed benchmark pair set, bucketed by optimal distance
- The full metrics table across policies and buckets

**Exit:** a results table that answers the actual question — how much of the
Wiki Game is pathfinding and how much is semantics — with reproducible numbers
behind it.

---

## Later, explicitly not now

Worth wanting, deliberately deferred:

- One-hop lookahead and an LLM policy for the fair side
- A web UI over `/paths`
- Alternative dense-id orderings (by title, by degree) as a locality experiment
- A `body_prose` link rule, with the matching pipeline edge filter
- Rewriting `sqldump.py` in Rust — **only** after it is measured as the
  bottleneck
- Exact `u128` path counts, if the `f64` approximation ever turns out to matter
  (ADR-015)
- Other language editions
