# HTTP API

The Rust service. Reads one immutable graph directory, answers path queries.

Everything here is a contract with two consumers: the bot (machine) and whatever
UI or curl session a human points at it. Response shapes are stable within a
graph format version.

---

## Title normalization

Applied to every title-shaped input, in this order:

1. If the input looks like a Wikipedia URL, take the article part — the segment
   after `/wiki/`, or the `title=` query parameter of `index.php`.
2. Percent-decode (`%20`, `%C3%A9`, …).
3. Drop everything from the first `#` (section fragments).
4. Strip a single leading `:`.
5. Replace `_` with a space.
6. Collapse runs of whitespace to one space; trim both ends.
7. Normalize to Unicode **NFC**.
8. Uppercase the first character. (enwiki runs with `$wgCapitalLinks = true`,
   so `banana` and `Banana` are the same article — but `BANANA` is not.)

Note what is *not* done: the rest of the string keeps its case, and no accent or
punctuation folding happens. `Zurich` and `Zürich` are different keys, related
only if a redirect exists — which the index already carries.

> This function is implemented **twice**, here in Rust and in the pipeline in
> Python, and the published index is sorted by the Python version's output while
> lookups binary-search using the Rust version's. A disagreement is a silent
> wrong answer, not a crash. Both are tested against
> `fixtures/normalization.jsonl`, which is the authority; add a case there
> before changing either implementation.

---

## Resolution

A normalized title is looked up by binary search over `index.keys` (see
[ARCHITECTURE §6](../ARCHITECTURE.md#6-data-model)). The index contains
redirect titles as well as canonical ones, so following a redirect costs
nothing extra — the key simply points at the target's dense id.

Every endpoint that takes a title returns a **resolution block**:

```json
{
  "input": "https://en.wikipedia.org/wiki/NYC#Culture",
  "normalized": "NYC",
  "canonical": "New York City",
  "redirected": true,
  "dense_id": 645042
}
```

`redirected` tells the caller the article they named is not the one they got,
which matters to the bot: a path through `NYC` and a path through
`New York City` are the same path.

---

## `GET /paths`

The main endpoint.

### Parameters

| Name | Type | Default | Notes |
|---|---|---|---|
| `from` | string | — | required; title or URL |
| `to` | string | — | required; title or URL |
| `n` | int | `20` | max distinct paths to return; capped at `200` |
| `max_extra` | int | `1` | how far past `d` to top up; `0` disables the top-up |
| `seed` | int | — | overrides the derived RNG seed; for tests only |

### Success

```json
{
  "graph_version": "20261001",
  "from": { "input": "Harvard University", "normalized": "Harvard University",
            "canonical": "Harvard University", "redirected": false, "dense_id": 18426 },
  "to":   { "input": "Banana", "normalized": "Banana",
            "canonical": "Banana", "redirected": false, "dense_id": 3312 },

  "distance": 2,
  "total_shortest_paths": 1843,
  "total_is_approximate": false,

  "paths": [
    { "length": 2, "titles": ["Harvard University", "Fruit", "Banana"] },
    { "length": 2, "titles": ["Harvard University", "Botany", "Banana"] }
  ],

  "includes_longer": false,
  "topup_truncated": false,

  "stats": {
    "cache": "miss",
    "nodes_visited": 41233,
    "edges_touched": 312004,
    "frontier_sizes": { "forward": [1, 212], "reverse": [1, 40820] },
    "ms": { "resolve": 0.3, "search": 11.2, "count": 1.4, "sample": 0.6, "total": 13.5 }
  }
}
```

Field notes:

- `paths` carries an explicit per-path `length` because the list may be
  heterogeneous. It may be **shorter than `n`** — fewer paths exist, duplicate
  draws were discarded, or the top-up was truncated.
- `total_shortest_paths` counts length-`d` paths only, and can be astronomically
  larger than `paths.length`. Above `2⁵³` it is rounded and
  `total_is_approximate` is `true`; see
  [`algorithms.md §3`](algorithms.md#overflow-is-real-not-theoretical).
- `includes_longer` means some returned paths are longer than `distance`. The
  sample is then **stratified, not uniform**: uniform within each length, not
  across the union.
- `topup_truncated` means the `d+1` fill ran out of its own budget. The shortest
  paths are still exact and complete — only the top-up was cut short.

### No path — a `200`, not an error

"These articles are not connected" is a correct answer, so it is not an error
response:

```json
{
  "graph_version": "20261001",
  "from": { "...": "..." },
  "to": { "...": "..." },
  "distance": null,
  "reachable": false,
  "unreachable_reason": "target_has_no_in_links",
  "total_shortest_paths": 0,
  "paths": [],
  "stats": { "...": "..." }
}
```

`unreachable_reason` is one of `no_path`, `source_has_no_out_links`,
`target_has_no_in_links`. The last two are detected from CSR offsets without
searching.

`A == B` returns `distance: 0` with a single one-element path.

---

## `POST /resolve`

Batch resolution. The bot calls this every turn with the whole link list of the
current page, so it must be one request rather than hundreds.

```json
{ "titles": ["Fruit", "Massachusetts_Institute_of_Technology", "NYC", "Some 2026 Topic"] }
```

```json
{
  "graph_version": "20261001",
  "resolved": [
    { "input": "Fruit", "normalized": "Fruit", "canonical": "Fruit",
      "redirected": false, "dense_id": 11221 },
    { "input": "Massachusetts_Institute_of_Technology", "normalized": "Massachusetts Institute of Technology",
      "canonical": "Massachusetts Institute of Technology", "redirected": false, "dense_id": 2049 },
    { "input": "NYC", "normalized": "NYC", "canonical": "New York City",
      "redirected": true, "dense_id": 645042 },
    { "input": "Some 2026 Topic", "normalized": "Some 2026 Topic", "canonical": null,
      "redirected": false, "dense_id": null }
  ]
}
```

An unknown title yields `null` rather than a `404` — in a batch, "not in this
dump" is expected, not exceptional. That is how the bot detects pages newer
than the graph.

`GET /resolve?title=…` does the same for one title and returns `404` with
suggestions, which is friendlier for debugging by hand.

---

## `POST /next-hop`

Used when the oracle's path pool is exhausted mid-game: of the links actually on
this page, which gets closest to the target?

```json
{ "target": "Banana", "candidates": ["Ivy League", "Fruit", "Massachusetts", "Some 2026 Topic"], "limit": 5 }
```

```json
{
  "graph_version": "20261001",
  "target": { "input": "Banana", "normalized": "Banana", "canonical": "Banana",
              "redirected": false, "dense_id": 3312 },
  "distance": 1,
  "best": [ { "input": "Fruit", "canonical": "Fruit", "dense_id": 11221, "distance": 1 } ],
  "unknown_candidates": ["Some 2026 Topic"],
  "stats": { "nodes_visited": 8812, "ms": { "total": 4.1 } }
}
```

All candidates at the minimum distance are returned, up to `limit`, so the
caller can break ties with its own policy. If a candidate *is* the target, it
comes back at `distance: 0` and nothing else is searched.

---

## `GET /suggest`

Prefix autocomplete, straight off the sorted index — binary search for the
prefix, walk forward.

`GET /suggest?q=harv&limit=10` →

```json
{ "graph_version": "20261001",
  "suggestions": ["Harvard", "Harvard Business School", "Harvard College", "Harvard University"] }
```

---

## `GET /healthz` and `GET /graph`

`/healthz` is for the load balancer: `200` once a graph is loaded and validated,
`503` before that.

`/graph` returns the active `manifest.json` plus runtime state — uptime, cache
hit rate, current concurrency, configured budgets. This is the endpoint that
answers "which dump am I actually talking to?", which is the first question to
ask when a path looks wrong.

---

## Errors

```json
{ "error": { "code": "search_budget_exceeded",
             "message": "search exceeded 20000000 nodes",
             "details": { "nodes_visited": 20000001, "limit": 20000000 } } }
```

| Status | `code` | When |
|---|---|---|
| `400` | `invalid_request` | missing `from`/`to`, `n` out of range, malformed body |
| `404` | `title_not_found` | single-title lookup failed; `details.suggestions` carries near matches |
| `429` | `too_many_requests` | rate limit, if one is configured |
| `503` | `search_budget_exceeded` | node or time budget hit; **no answer is better than a wrong `d`** |
| `503` | `graph_unavailable` | no graph loaded yet, or a reload is in flight and the old one was dropped |
| `500` | `internal` | a bug; the response carries a correlation id, not a stack trace |

Deliberately *not* errors: no path between the endpoints, fewer than `n` paths
available, a truncated top-up, an unknown title inside a batch `/resolve`.

---

## Caching and determinism

Cache key: `(graph_version, from_dense_id, to_dense_id, n, max_extra)`. Using
dense ids rather than raw input means `NYC` and `New York City` share an entry.

Because the RNG is seeded from `(graph_version, A, B)`, a cached response is
byte-identical to a recomputed one — so the cache cannot be the reason two
callers disagree. A graph swap changes `graph_version` and therefore every key,
which invalidates the cache by construction rather than by eviction.

An explicit `seed` parameter bypasses the cache.

---

## Concurrency and limits

Search is CPU-bound and must never run on a tokio worker. Requests pass through
a `tower` `ConcurrencyLimit` and the search itself runs on a `rayon` pool (or
`spawn_blocking`). The concurrency limit bounds scratch memory as well as CPU —
see [`algorithms.md §2`](algorithms.md#scratch-space) — so it is a real capacity
decision, not a formality.

Queueing beyond the limit sheds load with `429` rather than growing an unbounded
queue; a request that will be answered after the client has given up is pure
waste.

---

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `graph.dir` | `graphs/current` | directory, or the pointer to one |
| `graph.watch` | `true` | poll the pointer and hot-swap via `arc-swap` when it moves |
| `server.bind` | `127.0.0.1:8080` | |
| `server.concurrency` | `4` | simultaneous searches; also caps scratch memory |
| `search.max_nodes` | `20_000_000` | |
| `search.max_ms` | `2_000` | |
| `topup.max_nodes` | `5_000_000` | |
| `topup.max_ms` | `500` | |
| `cache.entries` | `10_000` | LRU over the key above |
| `paths.max_n` | `200` | ceiling on `n` |

Loaded from a TOML file, overridable by `WIKIPATHS_*` environment variables.
Every default above is a starting guess that should be replaced by a measured
number.

---

## Load-time validation

On startup and on every hot-swap, before the new graph becomes visible: check
file sizes against `manifest.json`, verify the `offsets` arrays are monotonic
with `offsets[N] == E`, spot-check that `targets` entries are `< N`, and confirm
the index is sorted. Full re-validation (the complete stage-9 suite) is the
pipeline's job; this is the cheap subset that catches a truncated file or a
mismatched pair of artifacts.

A failed check **aborts the swap and keeps the current graph serving.** Only a
failure at initial startup is fatal.
