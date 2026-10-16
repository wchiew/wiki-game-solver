# Bot and harness

Plays the Wiki Game using the pathfinder. Python, so the embedding and LLM
policies have a short path from idea to experiment.

The split that matters: **the harness owns the rules and the scoring; a policy
only chooses a link.** Everything interesting is a comparison between policies,
and comparisons are only meaningful if the rules are identical and external to
both.

> **Scope.** We only ever play our own harness against the MediaWiki API. No
> automated play against third-party multiplayer Wiki Game sites.

---

## Harness

### Rules

Configuration, not code — these are the knobs that define a game variant:

| Rule | Default | Notes |
|---|---|---|
| `allow_back` | `false` | whether the policy may return to a previous page |
| `click_limit` | `30` | hard stop; a run that hits it is a loss |
| `links_source` | `pagelinks` | which links count (below) |
| `allow_newer_than_dump` | `target_only` | `never`, `target_only`, or `always` |
| `revisit_penalty` | none | optional, for policies that loop |

**`links_source` is the subtle one.** Two defensible definitions:

- `pagelinks` — every ns0 link MediaWiki records for the page, via
  `action=query&prop=links&plnamespace=0`. This includes links injected by
  templates and navboxes.
- `body_prose` — only links inside the article body, excluding navboxes,
  infobox chrome, and category lists. Closer to what a human would consider
  "on the page".

Default to `pagelinks`, because the offline graph is built from exactly that
table. Choosing `body_prose` would make the oracle's graph a superset of the
harness's legal moves, so the "optimal" distance would sometimes be
unachievable — a judge that scores against an impossible target is worse than a
slightly generous link rule. `body_prose` stays available for a more
human-faithful variant, and if it is used, the pipeline needs a matching edge
filter.

### Game loop

```
state = { current: A, visited: [A], clicks: 0 }
while state.current != B and state.clicks < click_limit:
    links = fetch_links(state.current)         # cached / replayed
    legal = harness.filter(links, state)       # back button, visited, newer-than-dump
    choice = policy.choose(state.current, legal, B)
    if choice is None: return Outcome.GAVE_UP
    state.advance(choice)
return WON if state.current == B else LOST
```

The harness, not the policy, applies `filter`. A policy that tries to return an
illegal link gets an error, which is how policy bugs surface immediately rather
than as mysteriously good scores.

### Scoring

The pathfinder is also the judge: `optimal = GET /paths(A, B, n=1).distance`.

| Metric | Definition |
|---|---|
| `clicks` | links followed to reach `B` |
| `stretch` | `clicks / optimal` — 1.0 is perfect |
| `success_rate` | fraction of runs reaching `B` inside `click_limit` |
| `api_calls` | pathfinder calls per game (oracle cost) |
| `fetches` | live page fetches per game (politeness cost) |
| `cache_hit_rate` | on live fetches |

`optimal` is the optimum **in the dump**, which may differ from live Wikipedia.
Runs where the policy beats `optimal` are therefore possible and interesting —
they mean a link exists live that the graph does not have. Log them rather than
clamping `stretch` to ≥ 1.

### Run records

Every run writes one JSON line: rules, policy name and config, `A`, `B`,
`graph_version`, the full click sequence, outcome, metrics, and per-turn timing.
That file is the unit of analysis; nothing is reported that cannot be traced
back to it.

---

## Policy interface

```python
class Policy(Protocol):
    def start(self, source: str, target: str) -> None: ...
    def choose(self, current: str, links: list[str], target: str) -> str | None: ...
    def observe(self, chosen: str, outcome: StepOutcome) -> None: ...
```

`start` is where a policy may do expensive setup — the oracle's initial
`/paths` call. `choose` returning `None` means "I give up", which is a distinct
and useful outcome from losing on the click limit.

---

## Oracle policy

Has the graph. The upper bound on achievable play.

```
start(A, B):
    pool = GET /paths(A, B, n=20).paths        # a varied sample, unranked
    position = 0

choose(current, links, target):
    resolved = POST /resolve(links)            # one call, canonical ids
    if target in resolved: return target       # always take the win

    # the next hop of each pool path that is actually clickable here
    votes = Counter()
    for path in pool:
        if path[position] == current and position + 1 < len(path):
            hop = path[position + 1]
            if hop in resolved: votes[hop] += 1

    if votes:
        best = max(votes)                      # ties broken deterministically
        pool = [p for p in pool if p[position + 1] == best]   # prune disagreeing paths
        position += 1
        return best

    # pool exhausted or diverged from live reality
    hop = POST /next-hop(target, candidates=resolved)
    pool = GET /paths(hop, target, n=20).paths
    position = 0
    return hop
```

Three things this gets right:

- **Take the target whenever it is linked.** The graph may not know about that
  link — it is newer than the dump — but clicking it still wins. This is the one
  place where live reality always overrides the graph.
- **Vote, then prune.** Following a single path means one stale link ends the
  run. Voting across the pool picks the hop with the most surviving options
  behind it, and pruning keeps the pool consistent with where the bot actually
  is.
- **Re-plan instead of failing.** When the pool dies, `/next-hop` re-anchors from
  the links genuinely on the page and the pool is refilled from there. This is
  the path-not-found recovery, and it is why `/next-hop` exists as an endpoint
  rather than being folded into `/paths`.

The failure mode to watch is **pool collapse**: pruning reduces the pool to one
path within two clicks, after which a single stale link forces a re-plan.
Whether a larger `n`, or re-querying `/paths` from the current page each turn,
beats pruning is an experiment the eval harness should settle rather than a
guess to encode.

---

## Fair policy

Sees only the current page. No graph, no oracle.

- **Scoring.** Embed the target title once; embed each link title; pick the
  highest cosine similarity. A small local sentence-embedding model, so a turn
  costs milliseconds and no network.
- **Visited set.** Never revisit; this alone prevents most oscillation.
- **Hub heuristic.** Semantic similarity is poor at long range — from a tiny
  village stub, nothing on the page resembles `Quantum field theory`. So when
  the best similarity is below a threshold, prefer a broad hub (country, era,
  broad field) and narrow once similarity picks up. "Go general, then specific"
  is the actual human strategy, and the threshold is what encodes it.
- **Stretch goals.** One-hop lookahead (fetch each candidate's links and score
  the best grandchild — expensive, so the budget is the interesting part); an
  LLM policy choosing from the link list.

---

## Live pages

MediaWiki API, politely:

- `action=query&prop=links&plnamespace=0&pllimit=max`, following `continue`.
- A descriptive `User-Agent` with contact information, as the API etiquette
  guide requires.
- **Sequential** requests, no concurrency, with a small delay between them.
- Honour `maxlag`, and back off on `429` / `5xx` with exponential backoff.
- On-disk cache keyed by `(title, revision_id)`, so a cached page is
  unambiguous rather than merely recent.

### Record and replay

Every fetch is recorded. Evaluation runs execute in **replay mode**, where a
cache miss is an error rather than a fetch. That makes a benchmark run
reproducible, fast, and free of load on Wikimedia's servers — the three things
that otherwise make policy comparison unreliable.

Re-recording is explicit (`--refresh`), and the record set's capture date goes
into the run record, because a benchmark captured against one dump is not
comparable to one captured against another.

---

## Evaluation

**Pairs.** A seeded random sample of `(A, B)` pairs, bucketed by optimal
distance — roughly equal counts at `d = 2, 3, 4, 5, 6+` — so the hard buckets
are not drowned out by the easy ones. The pair list is committed, not generated
per run, so numbers are comparable across commits.

**Per bucket, per policy:** success rate, mean and median stretch, mean clicks,
API calls, fetches.

```
policy        d=2        d=3        d=4        d=5       overall
            succ str   succ str   succ str   succ str   succ  str
oracle      1.00 1.00  1.00 1.02  0.99 1.08  0.97 1.15  0.99 1.06
fair-embed  0.92 1.31  0.74 1.88  0.51 2.54  0.28 3.10  0.61 2.21
```

(Illustrative layout, not results.)

**What the numbers are for.** The oracle's stretch above 1.0 measures how much
the dump's staleness costs; the fair policy's gap to the oracle measures how much
of the game is pathfinding versus semantics. Both are the actual research
questions, and both need the harness, the judge, and the replay cache to be
trustworthy before any of it means anything.

---

## Running it

```bash
just play FROM="Harvard University" TO="Banana" POLICY=oracle
just eval POLICY=oracle,fair-embed REPLAY=1
just record PAIRS=benchmarks/pairs-v1.jsonl     # refresh the fetch cache
```
