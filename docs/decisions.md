# Decision records

Short records of the choices that shaped the design, each with the alternative
that was rejected and why. The point of writing these down is that six months
from now the *reasons* are the part that will have evaporated, not the choices.

Status values: **accepted** (decided, may not be built yet), **provisional**
(decided but expected to be revisited after measurement), **superseded**.

---

## ADR-001 · Dumps, not scraping

**Status:** accepted

**Context.** Building the graph needs every article's link list — on the order
of 7.25M pages.

**Decision.** Build the graph from Wikimedia SQL dumps. Fetch live pages only
during gameplay, through the MediaWiki API, sequentially, with a descriptive
User-Agent and an on-disk cache.

**Consequences.** The graph is a snapshot and lags live Wikipedia by days to
weeks, so staleness becomes a first-class concern the bot has to reason about
(see ADR-010). In exchange the build is reproducible, offline, and does not put
7 million requests on someone else's servers.

**Rejected.** Crawling. Would take weeks, be unreproducible, and is an abuse of
a free service when the dumps exist for exactly this purpose.

---

## ADR-002 · Python + DuckDB for the build

**Status:** accepted

**Context.** The build is a handful of large joins, a redirect-chain
resolution, a dedupe, and a ~700M-row sort. It runs monthly at most.

**Decision.** Python for orchestration and parsing; DuckDB for the joins,
resolution, dedupe, and the external sort; numpy for the final CSR write.
**DuckDB is a build tool only and is never in the serving path.**

**Consequences.** The build is as fast as its slowest Python loop — specifically
the SQL-dump tokenizer, which has to stream ~7 GB of gzipped `pagelinks`. That
is the one component expected to need a rewrite. The rule is: measure first,
then rewrite *only* `sqldump.py` in Rust, keeping the stage boundaries and file
formats unchanged.

**Rejected.** Spark or similar (enormous operational weight for a single-machine
job); doing everything in Rust (the build is I/O- and sort-bound, and SQL
expresses the joins far more clearly than hand-written Rust would);
Postgres/SQLite as the build engine (no comparable out-of-core sort, and far
slower on this shape of work).

---

## ADR-003 · Rust for the query API

**Status:** accepted

**Context.** Queries are CPU-bound graph traversals over ~6.5 GB of arrays, with
a latency target in the tens of milliseconds and a hard rule against
`N`-sized per-query allocation.

**Decision.** Rust, with axum/tokio for HTTP, memmap2 + bytemuck for zero-copy
`&[u32]` views over the artifacts, arc-swap for the hot graph pointer, and
rayon for the search pool.

**Consequences.** More up-front cost than Go or Python, and the async/CPU-bound
boundary has to be respected explicitly (search never runs on a tokio worker).
In return: no GC pauses on a 6.5 GB working set, and the memmap-to-slice cast is
genuinely free.

**Rejected.** **Go** — simpler, but a multi-gigabyte live heap is exactly what a
tracing GC is worst at, and the `[]byte`→`[]uint32` story is less clean. **C++**
— comparable performance, but no equivalent of cargo/crates for this
scale of project, and the lifetime bugs around a hot-swapped memory map are
precisely what Rust rules out at compile time. **Python** — an order of
magnitude too slow for a 10⁶-node traversal per request.

---

## ADR-004 · CSR over memory-mapped files, not a graph database

**Status:** accepted

**Context.** The serving data is a static directed graph: ~7.25M nodes,
~700M edges.

**Decision.** Two CSR adjacency structures (forward and reverse) as raw
little-endian `u32` files, memory-mapped read-only. No database at query time.

**Consequences.** Neighbour lookup is two array reads and a slice. The OS page
cache does the caching, so there is no cache to tune, warm, or invalidate. The
cost is that the format is ours to validate — hence the nine invariants and the
stage-9 gate — and that any change to the layout is a rebuild.

**Rejected.** **Neo4j and friends** — orders of magnitude more memory and
latency per hop for a graph that never changes between builds; their value is
transactional mutation, which we do not need. **SQLite/DuckDB at serve time** —
row-oriented access per hop, and a query planner between us and an array index.
**Adjacency lists in a KV store** — serialization on every hop.

---

## ADR-005 · `u32` dense ids, renumbered from zero

**Status:** accepted

**Context.** MediaWiki `page_id`s are sparse and already near 80 million, while
only 7.25M ns0 articles exist.

**Decision.** Number non-redirect ns0 articles `0 .. N-1` in `page_id` order.
Dense ids are the only identifier the search ever touches; redirects never get
one.

**Consequences.** CSR indexing is direct, and every `N`-sized scratch array is
7.25M entries rather than 80M+ — a 10× saving on the arrays that dominate
per-query memory. The cost is one more identifier layer to keep straight, which
is why [ARCHITECTURE §6](../ARCHITECTURE.md#identifier-layers) spells out all
four. Dense ids are **not** stable across builds; nothing outside a single graph
version may persist them.

**Rejected.** Using `page_id` directly (wasteful scratch arrays, no benefit);
hashing titles to ids (loses the array-index property entirely).

---

## ADR-006 · Layered bidirectional BFS with uniform sampling, not k-shortest-paths

**Status:** accepted

**Context.** We need `n ≈ 20` *varied, unranked* shortest paths, and the total
number of shortest paths between two enwiki articles is routinely in the
billions.

**Decision.** Bidirectional BFS expanded one complete layer at a time, recording
all parents; count paths by DP over the resulting DAG; sample uniformly from the
counts. See [`algorithms.md`](algorithms.md).

**Consequences.** We get the exact distance, an exact total count, and a
provably uniform sample without enumerating anything — which is the only reason
a pair with 10¹² shortest paths is answerable. The price is that the counts
overflow `u64`, forcing the `f64`-with-approximation-flag design, and that the
`d+1` top-up needs a separate walk-counting pass.

**Rejected.** **Yen's k-shortest-paths** — designed to produce the `k` *best*
paths in order, which is ranking; it is far more expensive and gives a biased,
clustered sample when all `k` have identical length. **Repeated randomized DFS**
— no uniformity guarantee, and tends to return paths sharing long prefixes.
**Enumerate then subsample** — impossible at these counts.

---

## ADR-007 · Drop self-links in the build

**Status:** accepted

**Context.** Some articles link to themselves, usually via a template.

**Decision.** Drop `src == dst` edges in pipeline stage 6, and make "no
self-links" a validated invariant.

**Consequences.** This is **load-bearing, not hygiene.** With no self-loops,
every walk of length `d+1` from `A` to `B` is provably a simple path
([`algorithms.md` Lemma 3](algorithms.md#5--topping-up-with-length-d1-paths)), so
the top-up can count and sample *walks* — a cheap linear DP — instead of doing
self-avoiding enumeration. The invariant check exists specifically so that a
future change to stage 6 cannot quietly invalidate the proof.

---

## ADR-008 · Sort the title index by bytes; share normalization test vectors

**Status:** accepted

**Context.** Resolution needs normalized-title → dense id over ~18M keys
including redirects, and `normalize()` necessarily exists in both Python (build)
and Rust (serve).

**Decision.** Store keys as a sorted UTF-8 blob plus an offsets array, looked up
by binary search directly on the mapped bytes. Sort by raw UTF-8 bytes. Both
implementations of `normalize()` are tested against one shared vector file,
`fixtures/normalization.jsonl`.

**Consequences.** No hashing, so no collision handling to get wrong, and
`/suggest` prefix search is free because the keys are already sorted. The real
risk moves to the cross-language contract: a disagreement between the two
`normalize()` implementations is a *silent wrong answer*, not a crash. The
shared vector file is the mitigation, and it is the authority — a case goes in
there before either implementation changes.

**Rejected.** A hash map in a file (collisions, no prefix search); an FST
(elegant, smaller, but another dependency and another format to validate, for a
lookup that is already fast); a database (ADR-004).

---

## ADR-009 · The dump date is the version; publish by atomic pointer flip

**Status:** accepted

**Context.** Rebuilding must not interrupt serving and must be reversible.

**Decision.** Artifacts live in `graphs/<DUMP_DATE>/`; `graphs/current` is a
pointer that is flipped atomically after validation. The dump date appears in
every response and every cache key. The API watches the pointer and hot-swaps
via `arc-swap`.

**Consequences.** A bad build is never half-live, rollback is another flip, and
cache invalidation happens by construction rather than by eviction. Old
directories accumulate and need pruning. On Windows the pointer is a directory
junction or, failing that, a one-line text file — both are atomic replaces, and
the API accepts either.

**Rejected.** Overwriting files in place (a bad build is live and unrecoverable);
restarting the service to pick up a new graph (downtime, and a cold page cache
on every deploy); a version number separate from the dump date (two things to
keep in sync, for no information gain).

---

## ADR-010 · Staleness is handled explicitly, not hidden

**Status:** accepted

**Context.** The graph lags live Wikipedia, so the bot will see links the graph
lacks and vice versa.

**Decision.** Every response carries its `graph_version`. The bot ignores live
links to pages newer than the dump — **except** when the link is the target,
which is always clicked. The harness records when a policy beats the "optimal"
distance instead of clamping it.

**Consequences.** Benchmark numbers are interpretable: oracle stretch above 1.0
*is* the measurement of staleness cost, and a run beating optimal is a signal
rather than a bug. The oracle also needs the `/next-hop` re-planning path
(ADR-011), because a stale link will eventually kill a path pool.

**Rejected.** Patching the graph with live data mid-game (destroys
reproducibility and the judge's independence); pretending the dump is current
(the bot would die on the first stale link with no recovery).

---

## ADR-011 · `/next-hop` as its own endpoint

**Status:** accepted

**Context.** Mid-game, the oracle's path pool can be exhausted — every planned
next hop is missing from the live page. It then needs: of the links *actually
here*, which is closest to the target?

**Decision.** A separate `POST /next-hop` taking a target and a candidate list,
answered by a reverse BFS from the target that stops at the first layer touching
a candidate.

**Consequences.** Recovery is one cheap call instead of `|candidates|` calls to
`/paths`, and the search is a single-sided BFS rather than a full bidirectional
one with path counting. It is roughly one side of the main search with a
membership test swapped in for the meeting test, so it reuses the same
machinery.

**Rejected.** Calling `/paths` once per candidate (tens of full searches per
turn); giving up and restarting the game (loses the run and the measurement).

---

## ADR-012 · Python for the bot

**Status:** accepted

**Context.** The fair policy needs sentence embeddings and possibly an LLM; the
eval needs quick data wrangling.

**Decision.** Python, consuming the API over HTTP.

**Consequences.** Three languages in the repo — but the bot is latency-tolerant
(a live page fetch dwarfs any local compute), and the ML ecosystem is where the
interesting policy work actually happens. The HTTP boundary keeps the Rust
service from growing a Python dependency.

**Rejected.** Rust for the bot (fights the ML tooling for no latency benefit);
folding the bot into the API (couples the judge to the player, which is exactly
what must stay separate).

---

## ADR-013 · Develop against Simple English Wikipedia

**Status:** accepted

**Context.** An enwiki build takes hours. Iterating on stage 6 at that cadence
is not viable.

**Decision.** The pipeline is wiki-parameterized and developed on `simplewiki`
first, with hand-built miniature dumps in `fixtures/tiny/` for unit tests.
Nothing runs against enwiki until the full chain is green on `simplewiki`.

**Consequences.** A full-chain integration test in minutes and stage tests in
milliseconds, which is what makes it reasonable to keep changing the pipeline.
`simplewiki` will not exercise the enwiki-scale paths — specifically the
streaming CSR build and the `u32` width guards — so those need their own tests
and an acceptance run.

---

## ADR-014 · Paths are not ranked

**Status:** accepted

**Context.** It is tempting to prefer "interesting" paths — through famous
articles, or avoiding obscure stubs.

**Decision.** No ranking. `/paths` returns an unbiased sample, and any
preference is the caller's to apply.

**Consequences.** Keeps the API honest about what it computed, keeps the uniform
sampling guarantee meaningful, and keeps the oracle's measurements clean — a
ranked pool would make the bot's results a measurement of the ranking heuristic
rather than of pathfinding. A ranking layer can be added on top later, over the
sample, without touching the search.

---

## ADR-015 · `f64` path counts with an approximation flag

**Status:** provisional

**Context.** Shortest-path counts between well-connected articles exceed `u64`.

**Decision.** Accumulate counts in `f64`; report the total as an exact integer
below `2⁵³` and as a flagged approximation above it.

**Consequences.** One array, one code path, and no overflow until 10³⁰⁸.
Sampling correctness is unaffected because it depends only on ratios. The
reported total becomes inexact for the largest pairs, which is a real if minor
loss of fidelity.

**Revisit if** exact counts for very large pairs turn out to matter — then
`u128` with saturating addition, at double the memory and with a saturation flag
of its own.
