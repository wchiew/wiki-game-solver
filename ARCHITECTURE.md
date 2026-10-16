# Architecture

How the three subsystems fit together, what contracts hold between them, and why
the shape is what it is. Detail lives in `docs/`; this file is the map.

- [`docs/pipeline.md`](docs/pipeline.md) — the offline build, stage by stage
- [`docs/algorithms.md`](docs/algorithms.md) — search, counting, sampling, proofs
- [`docs/api.md`](docs/api.md) — the HTTP contract
- [`docs/bot.md`](docs/bot.md) — harness, policies, evaluation
- [`docs/decisions.md`](docs/decisions.md) — decision records and rejected alternatives
- [`docs/roadmap.md`](docs/roadmap.md) — milestones and exit criteria

---

## 1. Problem

Given two English Wikipedia articles `A` and `B`, return **many
approximately-shortest paths** from `A` to `B` by following article links, fast
enough to serve interactively. Then use that to play the Wiki Game — and to
judge how well a player did.

### Functional requirements

| # | Requirement |
|---|---|
| F1 | Resolve a user-supplied title or URL to a canonical article, following redirects |
| F2 | Return the exact shortest-path distance `d` from `A` to `B` |
| F3 | Return up to `n` distinct shortest paths, sampled **uniformly** from all of them |
| F4 | If fewer than `n` shortest paths exist, top up with paths of length `d + 1` |
| F5 | Report the total number of shortest paths, not just the sample |
| F6 | Given a target and a set of candidate links, return the best next hop |
| F7 | Rebuild from a new dump without downtime and without changing query code |

Paths are **not ranked**. The goal is a good, varied, unbiased sample — ranking
("which path is most interesting") is a different problem and explicitly out of
scope.

### Non-functional requirements

| # | Requirement |
|---|---|
| N1 | Query latency: p50 < 50 ms, p99 < 500 ms for `d ≤ 4` on a warm page cache (*target; to be measured*) |
| N2 | No per-query allocation proportional to \|V\| (~7.25M) |
| N3 | Bounded work per query — every search has an explicit node and time budget |
| N4 | Deterministic: the same query against the same graph version returns the same paths |
| N5 | Every build is reproducible, restartable, and validated before publish |
| N6 | Serving reads only immutable memory-mapped files — no database at query time |

### Non-goals

- Ranking or scoring paths.
- Live graph updates. The graph is a snapshot; staleness is handled, not avoided.
- Namespaces other than article space (ns0).
- Languages other than English (Simple English is used as a development fixture).
- Playing on third-party multiplayer Wiki Game sites. We only run our own harness.

---

## 2. System overview

```
  Wikimedia dumps (one dump run, ~11 GB gzipped)
  page.sql.gz   pagelinks.sql.gz   linktarget.sql.gz   redirect.sql.gz
         |
         |  OFFLINE  ·  Python + DuckDB  ·  hours, run per dump
         v
  +--------------------------------------------------------------+
  |  pipeline/   10 file-to-file stages, each re-runnable         |
  |  parse -> Parquet -> join/resolve/dedupe -> CSR -> validate   |
  +--------------------------------------------------------------+
         |
         |  publish: atomic pointer flip
         v
  graphs/<DUMP_DATE>/          <-- immutable artifacts + manifest.json
  graphs/current  ------------>
         |
         |  memmap, zero-copy &[u32]
         v
  +--------------------------------------------------------------+
  |  api/   Rust · axum/tokio                                     |
  |  resolve -> bidirectional layered BFS -> count -> sample      |
  |  GET /paths   POST /next-hop   GET /resolve   GET /healthz    |
  +--------------------------------------------------------------+
         ^                                   ^
         |  oracle queries                   |  judge: optimal distance
         |                                   |
  +--------------------------------------------------------------+
  |  bot/   harness owns the rules; policies are pluggable        |
  |  oracle policy (uses the API)  ·  fair policy (current page)  |
  +--------------------------------------------------------------+
         |
         |  live page fetch, polite + cached
         v
  MediaWiki API (action=parse / action=query)
```

The three subsystems are deliberately separated by **files**, not by function
calls or a shared process:

- the pipeline's only output is a directory of bytes plus a manifest;
- the API's only input is that directory, opened read-only;
- the bot's only input is the API's HTTP surface and the live MediaWiki API.

That boundary is what makes each piece independently testable, and lets the
pipeline be slow and Python-shaped while the API is fast and Rust-shaped.

---

## 3. Subsystem: offline pipeline

**Language:** Python 3.12 + DuckDB + pyarrow + numpy. **Interface:** a Typer CLI
with one subcommand per stage, chained by `just`.

Ten stages, each reading files and writing files: fetch → pages → redirects →
dense ids → link targets → edges → sort/CSR → lookups → validate → publish.
See [`docs/pipeline.md`](docs/pipeline.md) for each stage's inputs, outputs,
logic, and tests.

Three properties matter more than speed:

1. **Re-runnable.** A stage is a pure function of its inputs plus the dump date.
   Delete its output, run it again, get the same content. (Content, not bytes —
   Parquet embeds writer metadata.)
2. **Restartable.** Multi-hour stages checkpoint to chunk files so a crash
   costs minutes, not hours.
3. **Validated before publish.** Stage 9 is a gate: structural invariants,
   golden fixtures, and drift-vs-previous-build. A build that fails the gate
   never becomes `current`.

DuckDB does the joins, redirect resolution, dedupe, and the large external sort
— it spills to disk, so the build is not bounded by RAM. **DuckDB is a build
tool only; it is never in the serving path.**

The one thing that may need rewriting is the dump parser: Wikimedia ships
MySQL `INSERT` statements, not CSV or Parquet, so a streaming tokenizer has to
turn ~7 GB of gzipped `pagelinks` SQL into Parquet. If that is the bottleneck,
only that step moves to Rust — and only after measuring.

---

## 4. Subsystem: query API

**Language:** Rust. **Stack:** axum/tokio, memmap2 + bytemuck, arc-swap, rayon.

### Request lifecycle

```
HTTP -> normalize title -> resolve to dense id -> cache lookup
     -> bidirectional layered BFS (CPU-bound, off the async executor)
     -> parent-DAG path counting (DP)
     -> uniform sampling (seeded RNG)
     -> optional d+1 top-up
     -> render titles -> JSON
```

### Why this search

A layered bidirectional BFS that records **all** parents gives three things at
once that a plain BFS or a k-shortest-paths algorithm (Yen's) does not:

- the exact distance `d`;
- a compact DAG containing *every* shortest path, with no path enumeration;
- an exact count of those paths, via a linear DP over the DAG.

From the count you get uniform sampling for free: pick a meeting node
proportional to `countA[m] · countB[m]`, then walk outward choosing each parent
proportional to its own count. Every shortest path comes out with probability
exactly `1 / total`. The proof — and the proof that no length-`d+1` walk can
repeat a vertex once self-links are dropped, which is what makes the top-up
sound — is in [`docs/algorithms.md`](docs/algorithms.md).

### Concurrency and memory discipline

- Graph arrays are memory-mapped and cast to `&[u32]` with zero copying. The OS
  page cache is the cache; there is no application-level graph cache.
- Search is CPU-bound, so it runs on a `rayon` pool (or `spawn_blocking`) behind
  a `tower` `ConcurrencyLimit`, never on a tokio worker.
- Per-query scratch comes from a **bounded pool of epoch-stamped arrays**: a
  query bumps a `u32` epoch and treats `visited[v] == epoch` as "seen", so
  nothing is cleared between queries and nothing \|V\|-sized is allocated per
  query. Pool size is tied to the concurrency limit, which is what bounds
  scratch memory.
- `arc-swap` holds the active graph. Publishing a new dump loads the new
  directory, validates it, and swaps the pointer; in-flight queries finish
  against the old `Arc` and the old mapping drops when the last one does.

---

## 5. Subsystem: bot and harness

The **harness** owns the rules — whether the back button is allowed, the click
limit, which links count as clickable — and scores a run against the optimal
distance from the API. The pathfinder is therefore both the oracle and the
judge; keeping those in one place is what makes the scores comparable.

A **policy** is just `choose(current_page, links, target) -> link`. Two are
planned:

- **Oracle** — pulls a pool of paths from `/paths` at game start, then each turn
  clicks the live-linked hop shared by the most pool paths, pruning paths that
  disagree. When the pool is exhausted, `POST /next-hop` finds the nearest live
  link to the target by reverse BFS and the pool is refilled from there.
- **Fair** — sees only the current page. Greedy embedding similarity between
  link titles and the target, a visited set, and a "broad hub first, then
  narrow" heuristic.

The gap between the two is the interesting measurement, and it is why both
policies must run under the same harness with the same rules.

Live pages come from the MediaWiki API with a descriptive User-Agent,
sequential polite requests, and an on-disk cache. Fetches are recorded so
evaluation runs replay instead of re-fetching. See [`docs/bot.md`](docs/bot.md).

---

## 6. Data model

This is the contract between the pipeline and the API. Everything is
little-endian and memory-mapped; nothing is parsed at startup.

### Identifier layers

Four distinct notions of "which article", and conflating them is the main
source of bugs in this kind of project:

| Layer | Example | Notes |
|---|---|---|
| User input | `https://en.wikipedia.org/wiki/new_york_city#History` | Arbitrary; may be a URL, may be a redirect |
| Normalized title | `New York City` | Percent-decoded, `_`→space, whitespace collapsed, first letter uppercased, NFC |
| MediaWiki `page_id` | `645042` | Stable across dumps, sparse, `u32`-sized but not dense |
| **Dense id** | `0 .. N-1` | Our own numbering over non-redirect ns0 articles. The only id the search sees. |

Redirects never get a dense id. They appear only as extra keys in the title
index, pointing at the dense id of their canonical target.

### Artifacts in `graphs/<DUMP_DATE>/`

| File | Type | Shape | Purpose |
|---|---|---|---|
| `forward.offsets` | `u32` | `N + 1` | CSR row starts, out-edges |
| `forward.targets` | `u32` | `E` | out-neighbour dense ids, sorted within row |
| `reverse.offsets` | `u32` | `N + 1` | CSR row starts, in-edges |
| `reverse.targets` | `u32` | `E` | in-neighbour dense ids, sorted within row |
| `titles.blob` | UTF-8 | — | canonical titles, concatenated in dense-id order |
| `titles.offsets` | `u32` | `N + 1` | byte offsets into `titles.blob` |
| `index.keys.blob` | UTF-8 | — | normalized titles, **lexicographically sorted**, canonical + redirect |
| `index.keys.offsets` | `u32` | `K + 1` | byte offsets into `index.keys.blob` |
| `index.vals` | `u32` | `K` | dense id per key, parallel to the keys |
| `manifest.json` | JSON | — | counts, checksums, dump date, code version |

Lookup is a binary search over `index.keys` directly on the mapped bytes — a
handful of page faults, no hashing, and no collision handling to get wrong.

Out-degree is `forward.offsets[v+1] - forward.offsets[v]` and in-degree is the
same over `reverse.offsets`, so **separate degree arrays are redundant**. They
are optional, and worth materializing only if profiling shows the indirection
matters. Sorted-title autocomplete reuses `index.keys`, which is already sorted,
so it needs no file of its own.

### Invariants (enforced by stage 9, re-checked by the API on load)

1. `offsets` is monotonically non-decreasing; `offsets[0] == 0`; `offsets[N] == E`.
2. Every entry of `targets` is `< N`.
3. No self-links: `u ∉ neighbours(u)`. **Load-bearing** — the `d+1` top-up
   proof depends on it.
4. No duplicate edges within a row.
5. Forward edge count equals reverse edge count, and the two CSRs are exact
   transposes.
6. `E < 2^32` and `N < 2^32` (guards the `u32` width; the format moves to `u64`
   offsets if `E` ever approaches the limit).
7. Every key in the title index resolves to a valid dense id.
8. Golden fixtures: a fixed set of known links and known paths still hold.
9. Drift: counts within a few percent of the previous build.

### Sizing — English Wikipedia

Order-of-magnitude only. **Stage 9 records the actual numbers in
`manifest.json`; these are estimates to size a machine, not facts.**

| Quantity | Estimate |
|---|---|
| `N` — ns0 non-redirect articles | 7.25M (7,250,570 as of Oct 2026) |
| ns0 redirects | ~11M |
| `K` — title index keys | ~18M |
| `E` — ns0→ns0 edges after dedupe | ~700M |
| Mean out-degree | ~95 |
| `forward.targets` / `reverse.targets` | ~2.8 GB each |
| `offsets` arrays | ~29 MB each |
| Titles + index | ~600 MB |
| **Total published artifacts** | **~6.5 GB** |
| Build working space | 50 GB+ free disk |

Serving wants the artifacts resident in page cache: 8 GB RAM is the practical
floor, 16 GB comfortable. Less still works — queries just take page faults.

---

## 7. Versioning and deployment

**The dump date is the graph version.** It appears in the artifact path, in
`manifest.json`, in every API response, and in every cache key. There is no
other version number for the data.

```
graphs/
  20261001/  manifest.json  forward.*  reverse.*  titles.*  index.*
  20261101/  ...
  current -> 20261101/
```

Publish is: write the new directory, validate it, then flip `current`
atomically. Flipping a pointer rather than overwriting files means a bad build
is never half-live, and rollback is another flip.

Cache keys are `(graph_version, A, B, n)`, so a swap invalidates the cache by
construction rather than by eviction. Determinism comes from seeding the RNG
from `(graph_version, A, B)` — the same question gets the same answer until the
graph changes, which is also what makes cached and uncached responses identical.

**Staleness is a real, visible condition, not a bug.** The graph lags live
Wikipedia by days to weeks. The bot therefore ignores live links to pages newer
than the dump — *unless* the link is the target, in which case clicking it wins
the game regardless of what the graph knows. Every response carries its
`graph_version` so the bot can reason about this.

---

## 8. Failure modes

| Condition | Behaviour |
|---|---|
| Title not found | `404` with normalized form and nearest-neighbour suggestions from the sorted index |
| `A == B` | `d = 0`, one trivial path |
| `A` has no out-links, or `B` has no in-links | No path, reported without searching |
| Genuinely disconnected | No path, after the frontier is exhausted |
| Search exceeds node or time budget | `503`-class **"search budget exceeded"** — explicit, never a silent truncation or a wrong `d` |
| Fewer than `n` shortest paths | Return all of them, top up with `d+1`, set `includes_longer` |
| Path count exceeds exact range | Report the total as approximate and set a saturation flag; sampling stays correct because it only needs ratios |
| Artifact fails load-time validation | Refuse the swap, keep serving the old graph, alarm |
| MediaWiki API error or rate limit | Bot backs off; harness records the run as errored, not as a loss |

N3 is the point of the budget row: it is better to say "I could not answer this
within budget" than to return a plausible-looking distance that is wrong.

---

## 9. Observability

Per query: `graph_version`, resolved endpoints, `d`, nodes visited per side,
frontier sizes per layer, total path count, paths returned, whether the `d+1`
top-up fired, cache hit, and wall-clock split across resolve / search / count /
sample. The same numbers go into the response `stats` block and into structured
logs, so a slow query can be explained from either side.

Per build: stage timings, row counts in and out of every stage, and the
drift report against the previous build.

For the bot: clicks versus optimal, success rate, API calls per game, and
cache hit rate on live page fetches.

---

## 10. Open questions

- Is the `d+1` walk count affordable on the worst real pairs, or does it need
  its own tighter budget than the shortest-path search? Measure before assuming.
- Dense epoch arrays cost ~29 MB per side per concurrent query. Is a hybrid that
  starts with a hash map and promotes to dense arrays once the frontier grows
  actually faster, or just more code? Measure.
- Do the two CSRs both need to be memory-resident, or is the reverse side cold
  enough in practice to leave to the page cache?
- How much does the oracle's advantage decay as the dump ages?
