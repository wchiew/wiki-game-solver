# wiki-game-solver

A Wikipedia pathfinder and a bot that plays the Wiki Game with it.

Given two English Wikipedia articles, find the shortest link distance between
them and return a varied, **uniformly sampled** set of shortest paths — not a
ranked top-`k`, a representative sample. Then use that to play the game, and to
judge how well anything else played it.

A learning project, built to practise software engineering and system design
fundamentals on a problem with real scale: ~7.25M articles and ~700M links, a
latency target in the tens of milliseconds, and no database at query time.

> **Status:** the pipeline's dump parser and stages 1–2 (`fetch`, `pages`) are
> built and tested; stages 3–10, the API, and the bot are not. See
> [`docs/roadmap.md`](docs/roadmap.md).

---

## The three parts

**1 · Offline pipeline** — ten re-runnable, restartable stages that turn four
Wikimedia SQL dumps into a compact memory-mappable link graph: forward and
reverse CSR adjacency, a title index covering redirects, and a manifest. Python
+ DuckDB; a build is validated before it is allowed to go live.

**2 · Query API** — a Rust service that memory-maps the graph and answers
`GET /paths?from=&to=&n=20`. A layered bidirectional BFS recording all parents
yields a DAG of every shortest path; a DP over it counts them exactly; the
counts give a provably uniform sample without enumerating anything — which is
what makes a pair with 10¹² shortest paths answerable at all.

**3 · Bot** — a harness that owns the game rules and scores runs against the
optimal distance, plus two policies: an **oracle** that plans with the API, and
a **fair** player that sees only the page it is standing on. The gap between
them is the measurement the project is actually for.

---

## Documentation

Read [`ARCHITECTURE.md`](ARCHITECTURE.md) first — it is the map, and it carries
the requirements, the data model, and the invariants. Then whichever of these is
relevant:

| Document | Contents |
|---|---|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | System overview, data model, on-disk formats, invariants, sizing, failure modes |
| [`docs/pipeline.md`](docs/pipeline.md) | The ten build stages, the dump schema traps, testing strategy |
| [`docs/algorithms.md`](docs/algorithms.md) | Search, counting, sampling — with the correctness proofs |
| [`docs/api.md`](docs/api.md) | HTTP contract, normalization rules, errors, configuration |
| [`docs/bot.md`](docs/bot.md) | Harness rules, policies, evaluation protocol |
| [`docs/decisions.md`](docs/decisions.md) | Why it is shaped this way, and what was rejected |
| [`docs/roadmap.md`](docs/roadmap.md) | Milestones with checkable exit criteria |

Two things in there are worth reading even if you skip the rest, because they
are the load-bearing arguments the whole design rests on:

- [why a complete BFS layer must finish before checking for a meeting](docs/algorithms.md#1--layered-bidirectional-bfs), and
- [why dropping self-links makes every length-`d+1` walk a simple path](docs/algorithms.md#5--topping-up-with-length-d1-paths)
  — which is what turns the "top up with slightly longer paths" feature from
  self-avoiding enumeration into a linear DP.

---

## Planned layout

```
pipeline/     Python · wikigraph  · dumps -> graph artifacts
api/          Rust   · wiki-paths · graph artifacts -> HTTP
bot/          Python · wikibot    · harness, policies, evaluation
fixtures/     tiny hand-built dumps, normalization vectors, golden paths
benchmarks/   committed benchmark pair sets
data/         downloaded dumps and build intermediates   (gitignored)
graphs/       published graph versions + `current` pointer (gitignored)
docs/
```

## Commands

The pipeline runs today; the rest is planned. Each subproject is driven by its
own CLI — there is no `make`/`just` layer to install.

```bash
cd pipeline
uv sync                                                 # Python 3.12 + deps

uv run wikigraph --help                                 # all ten stages
uv run wikigraph fetch --wiki simplewiki --date latest   # stage 1
uv run wikigraph pages --wiki simplewiki --date 20261001 # stage 2
uv run pytest -q && uv run ruff check .
```

Stages 3–10 are registered but exit 2 with a pointer to the roadmap. Planned,
once they exist:

```bash
uv run wikigraph build --wiki enwiki --date latest       # the whole pipeline
cargo run -p wiki-paths                                  # serve /paths
python -m wikibot play --from "Harvard University" --to Banana --policy oracle
python -m wikibot eval --policies oracle,fair-embed --replay
```

Development targets `simplewiki` — two orders of magnitude smaller and a
full-chain build in minutes. An enwiki build needs ~50 GB of free disk and
several hours; serving the result wants 8 GB of RAM, comfortably 16 GB.

---

## Data and attribution

Graph data derives from [Wikimedia dumps](https://dumps.wikimedia.org/).
Wikipedia content is licensed CC BY-SA 4.0 and GFDL; article titles and the link
structure in this project inherit that licensing.

Live page fetches go through the MediaWiki API with a descriptive User-Agent,
sequential polite requests, and an on-disk cache — per the
[API etiquette guidelines](https://www.mediawiki.org/wiki/API:Etiquette).
Evaluation runs replay from that cache rather than re-fetching.

The bot plays only against this project's own harness. It is not pointed at
third-party multiplayer Wiki Game sites.
