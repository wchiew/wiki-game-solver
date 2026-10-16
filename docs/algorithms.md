# Search, counting, and sampling

The core of the API. This file carries the correctness arguments, because two of
them are load-bearing and easy to get subtly wrong: the stopping rule for the
bidirectional search, and the claim that length-`d+1` walks are simple paths.

---

## Notation

| Symbol | Meaning |
|---|---|
| `A`, `B` | source and target, as dense ids |
| `out(u)`, `in(u)` | forward and reverse adjacency, read from the two CSRs |
| `distA[v]` | BFS distance `A → v` over forward edges; undefined if unvisited |
| `distB[v]` | BFS distance `v → B` over reverse edges |
| `dA`, `dB` | depths to which each side has been **completely** expanded |
| `d` | true shortest-path distance `A → B` |
| `M` | meeting set |
| `countA[v]` | number of distinct shortest paths `A → v` |
| `countB[v]` | number of distinct shortest paths `v → B` |

The graph is **directed** and has **no self-loops** (pipeline stage 6). Both
facts are used below.

---

## 1 · Layered bidirectional BFS

Expand whichever frontier is smaller, always one **complete** layer at a time,
and record *every* parent.

```
distA[A] = 0 ; distB[B] = 0
frontierA = {A} ; frontierB = {B}
dA = dB = 0

loop:
    if frontierA is empty or frontierB is empty:
        return UNREACHABLE

    if |frontierA| <= |frontierB|:
        expand_layer(forward, distA, parentsA, frontierA) ; dA += 1
    else:
        expand_layer(reverse, distB, parentsB, frontierB) ; dB += 1

    # only after the layer is COMPLETE
    M = { v : distA[v] and distB[v] are both defined }
    if M is non-empty:
        d = min over v in M of (distA[v] + distB[v])
        M = { v in M : distA[v] + distB[v] == d }
        return d, M

expand_layer(adj, dist, parents, frontier):
    next = {}
    for u in frontier:
        for v in adj(u):
            if dist[v] is undefined:
                dist[v] = dist[u] + 1
                next.add(v)
            if dist[v] == dist[u] + 1:        # new, or already found this layer
                parents[v].append(u)
    frontier = next
```

The parent condition is the whole trick. `dist[v] == dist[u] + 1` records `u`
for nodes discovered earlier in the *same* layer as well as newly discovered
ones, and rejects back-edges and same-layer edges. That one line is what turns a
BFS tree into a BFS **DAG** containing every shortest path.

### Why "complete layer" is not optional

Checking for a meeting mid-layer can return a distance that is too large.
Suppose, part-way through expanding A's layer, you touch a node `x` with
`distB[x]` already set, giving `distA[x] + distB[x] = 7`; later in the *same*
layer you would have touched `y` with a total of 6. Returning on first contact
reports 7. Finish the layer, then take the minimum.

### Correctness

**Lemma 1 (exact distances and complete parent DAG).** After complete expansion
to depth `dA`, `distA[v]` equals the true distance `A → v` for every `v` with
`distA[v] ≤ dA`, and `parentsA[v]` contains exactly those `u` with an edge
`u → v` and `distA[u] = distA[v] - 1`.

*Proof.* Standard BFS, plus the observation that `expand_layer` visits every
in-edge of every node in layer `k+1` from every node in layer `k`, and appends
`u` precisely when `distA[v] = distA[u] + 1`. ∎

A corollary is what the sampler needs: following `parentsA` backwards from any
`v` with `distA[v] ≤ dA` enumerates exactly the shortest `A → v` paths — all of
them, and nothing else.

**Lemma 2 (the stopping rule is sound).** If `d ≤ dA + dB`, then `M` is
non-empty and `min over M of (distA + distB) = d`.

*Proof.* Let `P = A = v₀ → v₁ → … → v_d = B` be any shortest path, and let
`i = min(dA, d)`. The prefix `v₀..v_i` is itself shortest, so `distA[v_i] = i ≤ dA`
and `v_i` is reached by A's side. Likewise `distB[v_i] = d - i`, and

- if `dA ≥ d` then `i = d` and `d - i = 0 ≤ dB`;
- otherwise `i = dA` and `d - i = d - dA ≤ dB` by assumption.

Either way `v_i` is reached by B's side too, so `v_i ∈ M` with
`distA[v_i] + distB[v_i] = d`. No member of `M` can total less than `d`, since
any `v ∈ M` yields a concrete path of length `distA[v] + distB[v]`. ∎

**Theorem.** The loop returns the true `d`, and `M` contains every node that
lies at offset `dA` along some shortest path.

*Proof.* When the loop returns, `M` is non-empty, so some `v ∈ M` gives a path
of length `distA[v] + distB[v] ≤ dA + dB`; hence `d ≤ dA + dB`, and Lemma 2
applies. Lemma 2's construction produces, for *every* shortest path, a witness
in `M` at offset `min(dA, d)`. Combined with Lemma 1, the pair of parent DAGs
restricted to `M` represents every shortest path exactly once, decomposed as
prefix × suffix. ∎

**Unreachability.** If `frontierA` empties, A's forward-reachable set is fully
explored and did not contain `B` (otherwise `B ∈ M`, since `distB[B] = 0`), so no
path exists. Symmetrically for `frontierB`.

### Cheap exits, taken before searching

- `A == B` → `d = 0`.
- `out(A)` empty, or `in(B)` empty → unreachable.

Both are one subtraction on the CSR offsets.

---

## 2 · Representing the parent DAG

Per-node `Vec`s would mean millions of tiny allocations. Instead, during a layer
expansion push `(child, parent)` pairs onto one flat buffer, and at the end of
the layer counting-sort them by child into a per-query CSR. One contiguous
buffer, one pass, no allocator pressure.

### Scratch space

`N ≈ 7.25M`, so nothing `N`-sized may be allocated or cleared per query (N2).
Two arrays per side are indexed by dense id and live in a pool:

| Array | Size | Purpose |
|---|---|---|
| `stamp` | `N × u32` = 29 MB | epoch marker; `stamp[v] == epoch` means "visited this query" |
| `slot` | `N × u32` = 29 MB | maps a visited node to its index in the compact per-query arrays |

Everything else — `dist`, `count`, the parent CSR, the frontier — is sized by
the number of *visited* nodes, not by `N`. A query bumps the epoch instead of
clearing anything, so setup is O(1).

That is ~58 MB per side, ~116 MB per concurrent query. **The concurrency limit
is therefore also the memory limit**, which is why it is an explicit config knob
rather than "number of cores". A hash-map variant avoids the fixed arrays and is
faster for tiny searches but loses badly once a frontier reaches ~10⁵ nodes;
whether a hybrid that promotes from map to array is worth the code is an open
question in [ARCHITECTURE §10](../ARCHITECTURE.md#10-open-questions).

Epoch wraparound at `u32` is handled by zeroing both arrays when the counter
laps — once every 4 billion queries, so the cost never shows up.

---

## 3 · Counting shortest paths

A linear DP over the DAG, in layer order:

```
countA[A] = 1
for k = 1 .. dA:
    for v in layer k of A's side:
        countA[v] = sum of countA[p] for p in parentsA[v]
```

Symmetrically for `countB`. Then

```
total = Σ over m in M of countA[m] · countB[m]
```

which is exact because the theorem above says every shortest path decomposes
uniquely as (prefix to `m`) × (suffix from `m`) for exactly one `m ∈ M`.

### Overflow is real, not theoretical

These counts multiply along the way. For `d = 5` between two well-connected
enwiki articles, totals in the 10¹²–10¹⁸ range are ordinary and larger is
possible. `u64` is not obviously safe and `u32` certainly is not.

**Use `f64`.** It is exact for integers up to `2⁵³ ≈ 9.0 × 10¹⁵`, degrades to
~10⁻¹⁶ relative error above that, and does not overflow until 10³⁰⁸. Sampling
only ever needs *ratios* of counts, so that error is irrelevant to correctness
of the sample. Reporting is where it matters, so the response distinguishes:

- `total_shortest_paths` — an integer, when the total is below `2⁵³`;
- `total_is_approximate: true` — above it, with the value rounded.

`u128` with saturating addition is the alternative: exact to `3.4 × 10³⁸`, at
twice the memory and a saturation flag anyway. `f64` is one array and one code
path; prefer it until something actually needs exact astronomical counts.

---

## 4 · Uniform sampling

One draw:

1. Pick `m ∈ M` with probability `countA[m] · countB[m] / total`.
2. Walk backwards from `m` to `A`: at node `v`, choose `p ∈ parentsA[v]` with
   probability `countA[p] / countA[v]`.
3. Walk forwards from `m` to `B`: at node `v`, choose `q ∈ parentsB[v]` with
   probability `countB[q] / countB[v]`.
4. Concatenate.

**Claim.** Every shortest path is produced with probability exactly `1 / total`.

*Proof.* Fix a shortest path `P`, with witness `m ∈ M`. Step 1 selects that `m`
with probability `countA[m]·countB[m] / total`. The backward choices along `P`
telescope:

```
countA[v_{i-1}]/countA[v_i] · countA[v_{i-2}]/countA[v_{i-1}] · … · countA[A]/countA[v_1]
    = countA[A] / countA[m] = 1 / countA[m]
```

since `countA[A] = 1`. The forward walk gives `1 / countB[m]` the same way.
Multiplying: `countA[m]·countB[m]/total · 1/countA[m] · 1/countB[m] = 1/total`.
The witness `m` is unique for `P`, so these events do not overlap. ∎

Note what this buys: a uniform sample of shortest paths **without enumerating
them**, which is the only reason a pair with 10¹² shortest paths is answerable
at all.

### Distinctness and determinism

Draws are independent, so duplicates happen. Draw until `n` distinct paths or
`4n` attempts, whichever first, and report however many came out — `paths` may
be shorter than `n`.

When `total ≤ max(n, 1024)`, skip sampling and enumerate the DAG by DFS
instead; it is cheaper and guarantees exactly `min(n, total)` distinct results.

The RNG is seeded from `(graph_version, A, B)`, so the same question gets the
same sample until the graph changes (N4). This is also what makes a cached
response byte-identical to a fresh one.

---

## 5 · Topping up with length-`d+1` paths

When `total < n`, fill the rest with paths of length `d + 1`.

**Lemma 3 (simplicity).** If `dist(A, B) = d` and the graph has no self-loops,
then **every walk of length `d + 1` from `A` to `B` is a simple path.**

*Proof.* Let `W = A = w₀ → … → w_{d+1} = B` and suppose `w_i = w_j` with
`i < j`. Then `w₀ … w_i, w_{j+1} … w_{d+1}` is also a walk from `A` to `B`, of
length `(d+1) - (j-i)`. Its length is at least `d`, since `d` is minimal, so
`(d+1) - (j-i) ≥ d`, giving `j - i ≤ 1`, hence `j = i + 1`. But then the edge
`w_i → w_{i+1}` is `w_i → w_i`, a self-loop, which the graph does not contain.
Contradiction. ∎

This is why stage 6 drops self-links. With Lemma 3 we may count and sample
**walks**, which is a simple matrix-power-style DP, and get valid simple paths
for free. Without it we would need a self-avoiding enumeration, which is a
different and far worse algorithm.

### Counting the walks

Pick a split `p + q = d + 1`, and define

- `WA_p[v]` = number of walks of length exactly `p` from `A` to `v`,
- `WB_q[v]` = number of walks of length exactly `q` from `v` to `B`.

Then `total_{d+1} = Σ_v WA_p[v] · WB_q[v]`. Sample `v` proportional to that
product and walk outward exactly as in §4, using `WA_{k-1}` restricted to
in-neighbours at each backward step. This requires keeping all intermediate
layers `WA_0 … WA_p`, which is the main memory cost of the top-up.

**The shortest-path counts cannot be reused.** `countA[v]` counts walks of
length `distA[v]`; `WA_p[v]` for `p > distA[v]` additionally counts walks that
waste steps. The top-up is a genuinely separate relaxation pass over a larger
support than the shortest-path DAG — which is exactly why it is the expensive
part.

**Which side gets the extra layer.** Put it where one more relaxation touches
fewer edges: compare the summed out-degree of A's last layer against the summed
in-degree of B's last layer, and extend the cheaper one. Both numbers are CSR
offset subtractions, so the estimate is nearly free.

### The top-up is best-effort

The support of `WA_p` grows faster than a BFS frontier, so the top-up can be
much more expensive than the search that preceded it. It therefore gets **its own
budget**, and running out is *not* an error: the shortest paths were already
computed correctly, so the response returns what it has with
`topup_truncated: true`. Only a failure of the shortest-path search itself is a
hard `search_budget_exceeded`.

Paths of length `d+1` are tagged in the response and never silently mixed with
shortest ones; `includes_longer` says the sample is heterogeneous. The `d`-paths
are uniform among `d`-paths and the `(d+1)`-paths are uniform among
`(d+1)`-paths, but the union is **not** a uniform sample of
`length ≤ d+1` paths — it is a stratified one. The API documents it that way
rather than pretending otherwise.

---

## 6 · Budgets

Every search carries a node budget and a wall-clock budget. Exhausting either
aborts with an explicit `search_budget_exceeded` rather than returning a
plausible-but-wrong distance. Separate, smaller budgets govern the `d+1`
top-up, which degrades rather than failing.

| Knob | Default (starting point, to be tuned) |
|---|---|
| `search.max_nodes` | 20,000,000 |
| `search.max_ms` | 2,000 |
| `topup.max_nodes` | 5,000,000 |
| `topup.max_ms` | 500 |

Checks go at layer boundaries and every few thousand nodes within a layer, so
the abort is prompt without putting a branch on the inner edge loop.

---

## 7 · `next-hop`

The bot needs this when its path pool is exhausted: *of the links actually on
this live page, which is closest to the target?*

Run a reverse BFS from `B`, layer by layer, and stop at the first layer
containing a candidate. Return every candidate at that minimum distance plus the
distance itself. Candidates absent from the graph — pages newer than the dump —
are reported in `unknown_candidates` and otherwise ignored, unless a candidate
*is* the target, which short-circuits everything.

This is the same machinery as one side of the main search, with a membership
test on the candidate set instead of a meeting test.

---

## 8 · Complexity

| Phase | Cost |
|---|---|
| Search | O(Σ degrees of expanded frontiers); bidirectional makes this roughly `b^(d/2)` instead of `b^d` |
| Parent DAG | O(edges into visited nodes) |
| Counting | O(visited + parent edges) |
| Sampling | O(n · d) expected |
| `d+1` top-up | O(Σ degrees over the support of `WA_p`) — the expensive one |

With mean out-degree ~95, a `d = 4` query is roughly two layers per side, on the
order of 10⁵–10⁶ nodes visited. Pairs needing `d = 6+` are where the budget
earns its keep.

---

## 9 · Testing

The arguments above are only worth as much as the tests that check the code
matches them.

| Test | Method |
|---|---|
| Distance correctness | compare against a plain BFS on random graphs (`N ≤ 200`) |
| Path-set completeness | brute-force enumerate all simple paths on random graphs (`N ≤ 12`); assert the DAG's path set equals the brute-force set of length-`d` paths |
| Count correctness | assert `total` equals the brute-force count |
| Sampling uniformity | draw 10⁵ samples on a fixture with a known, modest total; chi-square against uniform |
| Lemma 3 | assert every sampled `d+1` path has distinct vertices, on random graphs including ones where self-links were deliberately *kept*, to confirm the test catches the violation |
| Determinism | same `(graph_version, A, B, n)` twice → identical bytes |
| Budgets | a deliberately hard pair returns `search_budget_exceeded`, not a wrong answer |
| Python ↔ Rust agreement | a reference implementation in Python and the Rust one must agree exactly on committed fixtures |

The `N ≤ 12` brute-force comparison is the highest-value test here: it is the
only one that would catch a wrong parent-recording condition, and it runs in
milliseconds.
