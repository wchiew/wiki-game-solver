# Offline pipeline

Turns four Wikimedia dump files into the memory-mappable graph artifacts
described in [ARCHITECTURE.md §6](../ARCHITECTURE.md#6-data-model).

**Stack:** Python 3.12, DuckDB, pyarrow, numpy, Typer, pytest + Hypothesis.

---

## Conventions

Every stage is a `wikigraph` subcommand, and every stage is a function from
files to files:

```
wikigraph fetch      --wiki enwiki --date 20261001
wikigraph pages      --wiki enwiki --date 20261001
wikigraph redirects  ...
```

- **Layout.** Inputs and intermediates live under
  `data/<wiki>/<date>/{raw,work}/`; published output under `graphs/<date>/`.
  `data/` and `graphs/` are gitignored.
- **Re-runnable.** A stage's output depends only on its inputs plus
  `(wiki, date)`. Deleting an output and re-running reproduces the same
  *content* — Parquet embeds writer metadata, so byte-identical output is not
  something to promise or to test for.
- **Restartable.** Stages over the big tables (6 especially) write numbered
  chunk files plus a progress file, so a crash resumes at the last chunk.
- **Idempotent publish.** Stage 10 writes a new directory and flips a pointer;
  it never mutates a published build.
- **Observable.** Every stage appends `{stage, rows_in, rows_out, dropped_by_reason,
  seconds}` to `data/<wiki>/<date>/work/stats.jsonl`. Those numbers are what
  stage 9's drift check and `manifest.json` are built from.
- **Develop on Simple English.** `--wiki simplewiki` is ~2 orders of magnitude
  smaller and runs end-to-end in minutes. Nothing goes near `enwiki` until the
  whole chain is green on `simplewiki`.

---

## Source data

All four files must come from the **same dump run**. Mixing runs produces
dangling ids that look like data corruption much later.

| File | Size (enwiki, approx) | Columns we use |
|---|---|---|
| `page.sql.gz` | ~2.4 GB | `page_id`, `page_namespace`, `page_title`, `page_is_redirect` |
| `pagelinks.sql.gz` | ~7.1 GB | `pl_from`, `pl_from_namespace`, `pl_target_id` |
| `linktarget.sql.gz` | ~1.4 GB | `lt_id`, `lt_namespace`, `lt_title` |
| `redirect.sql.gz` | ~0.3 GB | `rd_from`, `rd_namespace`, `rd_title`, `rd_interwiki` |

### The schema change that breaks older tutorials

`pagelinks` **no longer carries `pl_namespace` / `pl_title`.** A row is now
`(pl_from, pl_from_namespace, pl_target_id)`, and the target identity lives in
the separate `linktarget` table. So an article-to-article edge requires a
three-way hop:

```
pagelinks.pl_target_id -> linktarget.lt_id
linktarget.(lt_namespace, lt_title) -> page.(page_namespace, page_title) -> page_id
page_id -> canonical page_id (redirects) -> dense id
```

Most pre-2024 write-ups join `pagelinks` straight to `page` on
`(pl_namespace, pl_title)`. That join no longer exists.

### The other trap: `redirect.rd_title` is a *title*, not an id

`redirect` gives you `rd_from` (a `page_id`) but identifies the target by
`(rd_namespace, rd_title)`. Resolution therefore needs the title→`page_id` map
from stage 2, and must filter to `rd_namespace = 0` with an empty
`rd_interwiki`.

### Parsing the dumps

Dumps are MySQL scripts — `DROP TABLE`, `CREATE TABLE`, then long runs of
`INSERT INTO <table> VALUES (...),(...),...;`. DuckDB cannot read them, so
stages 2, 3, 5, and 6 share one streaming tokenizer in
`wikigraph/sqldump.py`:

```python
def stream_rows(path: Path) -> Iterator[dict]:
    """Yield one dict per INSERT tuple, keyed by column name."""
```

Design notes that matter:

- **Read the DDL, do not hardcode column positions.** The `CREATE TABLE`
  statement sits at the top of the same file. Column order and the exact set of
  columns have changed across MediaWiki versions; parsing the DDL for names and
  order makes the parser survive the next change instead of silently reading
  the wrong field.
- **Escapes.** Values are single-quoted with MySQL escaping: `\'`, `\"`, `\\`,
  `\n`, `\r`, `\t`, `\0`, `\Z`, plus bare `NULL` and unquoted numerics. Titles
  routinely contain apostrophes and backslashes, so this is the single most
  bug-prone function in the pipeline and gets the most tests.
- **Streaming, never line-based.** A single `INSERT` statement can be a
  megabyte and is not newline-delimited in a useful way. Read fixed-size blocks
  from `gzip`, keep a tail buffer, emit complete tuples.
- **Titles use underscores** and exclude the namespace prefix. Converting
  `_`→space is normalization's job (stage 8), not the parser's.
- Output goes to Parquet via pyarrow in row-group batches, never into one
  in-memory table.

If profiling says this is the bottleneck, **only this module** gets rewritten in
Rust — see [`decisions.md`](decisions.md) ADR-002.

---

## Stages

### 1 · `fetch` — acquire and verify

**In:** `(wiki, date)` · **Out:** `raw/{page,pagelinks,linktarget,redirect}.sql.gz` (each with a `.sha1` marker), `raw/dumpstatus.json`

Download from `https://dumps.wikimedia.org/<wiki>/<date>/`.

**`dumpstatus.json` is the source of truth**, not `sha1sums.txt`: it carries
each file's `status`, `size`, `sha1` and `url` together, so one request gives
both the completeness check and the checksums. Files are located by *filename*
inside its jobs map, which avoids hardcoding job names like `pagelinkstable`.
`sha1sums.txt` is only a fallback for a run whose status omits a digest.

Refuse to proceed unless all four wanted files report `status: "done"` in the
same run — a half-written dump is the worst input because it fails late and
looks like a logic bug.

Downloads go to `<name>.part`, resume with HTTP `Range`, and are renamed only
once the checksum passes — so a file at its real path is always verified. A
`<name>.sha1` marker records the digest that passed, because re-hashing 7 GB on
every pipeline re-run is minutes of pure waste. A file present with the wrong
size is *discarded* rather than resumed: only an explicit `.part` is a
trustworthy prefix.

`--date latest` walks the dated directories newest-first and takes the first
complete run, then pins and prints that date. Wikimedia's own `latest/`
directory uses `-latest-` filenames and so hides the date, but the date *is*
the graph version.

**Tests:** checksum mismatch raises; resume continues rather than restarts;
incomplete `dumpstatus.json` is rejected with a readable message.

---

### 2 · `pages` — article inventory

**In:** `raw/page.sql.gz` · **Out:** `work/pages.parquet` — `page_id: uint32`, `title: string`, `is_redirect: bool`

Stream `page`, keep `page_namespace == 0`, project the four columns.

`title` holds the **raw dump form — underscores intact, no namespace prefix.**
Stage 3 joins `redirect.rd_title` against this column in exactly that spelling,
and normalization is stage 8's job, so converting here would silently break
that join.

**Checks:** `page_id` unique; `title` unique within ns0 (MediaWiki guarantees
this, so a violation means the parser is wrong, not the data). Both run against
the temporary file *before* it is moved into place, so a failed check publishes
nothing.

Titles that are not valid UTF-8 are counted and dropped. Mojibake would
otherwise reach the stage-8 index, where a disagreement with the Rust
normalizer is a silent wrong answer rather than a crash.

**Tests:** a fixture containing several namespaces, redirect and non-redirect
rows, and titles with embedded quotes, backslashes, and non-ASCII characters.

---

### 3 · `redirects` — canonicalize

**In:** `raw/redirect.sql.gz`, `work/pages.parquet` · **Out:** `work/canonical.parquet` — `page_id: uint32`, `canonical_id: uint32`

Join `rd_from` → `(rd_namespace = 0, rd_interwiki` empty`)` → `rd_title` →
`page_id` via the stage-2 title map. Then resolve chains iteratively up to
`--max-hops` (default 8), and:

- drop **cycles** (`R1 → R2 → R1`) and self-redirects;
- drop **broken** redirects whose target title is not an ns0 page;
- drop redirects still unresolved at the hop limit.

Non-redirect pages map to themselves, so downstream stages can apply
`canonical()` unconditionally.

> **Known fidelity gap.** MediaWiki does *not* follow double redirects at read
> time — click a link to `R1` where `R1 → R2 → X` and you land on `R2`, not
> `X`. Following chains makes our graph slightly more optimistic than the live
> site. Double redirects are rare and bots fix them, so we follow chains for
> simplicity and record `multi_hop_redirects` in the manifest so the size of
> the gap is visible rather than assumed.

**Tests:** chain of three; cycle; self-redirect; redirect to a non-existent
title; redirect to a non-ns0 title; hop limit exactly reached and exceeded.

---

### 4 · `dense-ids` — number the articles

**In:** `work/pages.parquet` · **Out:** `work/dense.parquet` — `page_id: uint32`, `dense_id: uint32`

Take ns0 pages with `is_redirect == false`, order by `page_id` ascending, number
`0 .. N-1`. Ordering by `page_id` is deterministic and free.

**Checks:** `N` equals the non-redirect ns0 count; dense ids are contiguous.

> The dense numbering is a free lever on memory locality, since it decides which
> adjacency rows sit on the same page. Ordering by title (alphabetical locality)
> or by degree (hubs together) are both plausible later experiments; `page_id`
> order is the neutral starting point. Changing the order changes every artifact,
> so it is a graph-format change, not a tweak.

---

### 5 · `link-targets` — resolve the indirection table

**In:** `raw/linktarget.sql.gz`, `work/pages.parquet`, `work/canonical.parquet`, `work/dense.parquet` · **Out:** `work/linktarget_dense.parquet` — `lt_id: uint64`, `dense_id: uint32`

Keep `lt_namespace == 0`; map `lt_title` → `page_id` → `canonical_id` →
`dense_id`. Drop rows where the title is not a known ns0 page (red links) or
the canonical target did not survive stage 3.

Because `canonical()` is applied here, **a link to a redirect becomes an edge to
the redirect's target** — which is exactly what a player experiences when they
click it.

**Checks:** record drop counts separately for red links and dead redirects;
these are the numbers stage 9 watches for drift.

---

### 6 · `edges` — the big join

**In:** `raw/pagelinks.sql.gz`, `work/dense.parquet`, `work/linktarget_dense.parquet` · **Out:** `work/edges/chunk-NNNN.parquet` — `src: uint32`, `dst: uint32`

Stream `pagelinks` and keep a row only if:

1. `pl_from_namespace == 0`, **and**
2. `pl_from` has a dense id — i.e. the source is a non-redirect ns0 article, **and**
3. `pl_target_id` resolves via stage 5, **and**
4. `src != dst`.

Condition 2 silently drops links *from* redirect pages, which is correct: a
redirect page's only link is to its own target, and admitting it would create a
spurious `redirect → target` edge. Links *through* a redirect were already
handled in stage 5.

Condition 4 is **load-bearing, not hygiene.** Dropping self-links is what makes
every length-`d+1` walk a simple path, which is what the top-up in
[`algorithms.md`](algorithms.md#5--topping-up-with-length-d1-paths) relies on.

Write a chunk every few million rows and record **how many input rows have been
consumed**, so a crash resumes instead of restarting a multi-hour pass.

Note what resume cannot be: Wikimedia dumps are single-member gzip, so a
*decompressed* byte offset is not seekable, and a compressed offset is not a row
boundary. Resuming therefore means re-decompressing from the start and skipping
the rows already done — a few minutes of streaming against hours of parsing,
which is a trade worth making rather than an index worth building.

**Tests:** self-link dropped; redirect-sourced link dropped; link through a
redirect lands on the canonical target; link to a red link dropped.

---

### 7 · `csr` — sort, dedupe, build both directions

**In:** `work/edges/*.parquet` · **Out:** `forward.offsets`, `forward.targets`, `reverse.offsets`, `reverse.targets`

DuckDB does the heavy part, spilling to disk so RAM is not the limit:

```sql
SELECT DISTINCT src, dst FROM read_parquet('work/edges/*.parquet')
ORDER BY src, dst;
```

Then stream the sorted result and append `dst` values to `forward.targets`
while accumulating per-row degrees; `offsets` is the exclusive prefix sum of
those degrees. Repeat with `ORDER BY dst, src` for the reverse CSR.

Streaming matters at enwiki scale: at `E ≈ 700M` the two edge columns alone are
~5.6 GB, so the `np.bincount` + `np.cumsum` formulation is the right one for
`simplewiki` and for tests, but the enwiki path must not materialize `src` in
memory. The same function should do both, driven by a size threshold.

**Checks:** invariants 1–6 of [ARCHITECTURE §6](../ARCHITECTURE.md#invariants-enforced-by-stage-9-re-checked-by-the-api-on-load),
including the `E < 2^32` guard on the `u32` offset width.

---

### 8 · `lookups` — titles and the resolution index

**In:** `work/pages.parquet`, `work/canonical.parquet`, `work/dense.parquet` · **Out:** `titles.blob`, `titles.offsets`, `index.keys.blob`, `index.keys.offsets`, `index.vals`

- `titles.*` — canonical titles in dense-id order, for rendering paths back to
  text.
- `index.*` — every way a user or a live page might name an article:
  `normalize(canonical_title)` for all `N` articles, plus
  `normalize(redirect_title)` for every redirect that survived stage 3,
  **sorted by raw UTF-8 bytes** of the normalized key, with `index.vals[i]`
  giving the dense id.

Two keys can collide after normalization (a redirect that differs from an
article only in something normalization folds). Policy: **a canonical article's
own title always wins**; among redirects, the lexicographically first
`page_title` wins, so the result does not depend on input order.

> **Cross-language contract.** `normalize()` exists twice — Python here, Rust in
> the API — and the index's sort order must match the API's comparison exactly
> or binary search silently misses. Both implementations are tested against a
> shared vector file, `fixtures/normalization.jsonl`, which is the authority.
> Rules are listed in [`api.md`](api.md#title-normalization).

---

### 9 · `validate` — the gate

**In:** everything from 7 and 8, plus the previous build's `manifest.json` · **Out:** `work/validation.json`, non-zero exit on failure

Three families, all must pass:

**Structural** — the nine invariants in
[ARCHITECTURE §6](../ARCHITECTURE.md#invariants-enforced-by-stage-9-re-checked-by-the-api-on-load).
The transpose check (forward and reverse are exact transposes) is the one most
likely to catch a real bug in stage 7.

**Golden fixtures** — a committed file of assertions that must hold on the real
wiki: specific links that exist, specific links that must not, and a handful of
known short paths. These catch semantic breakage that structural checks pass
clean, e.g. a redirect resolved to the wrong article.

**Drift** — compare `N`, `E`, redirect count, index key count, and mean degree
against the previous build. A change beyond a few percent fails the build. Real
month-over-month drift is well under 1%, so this reliably catches a stage that
quietly started dropping rows.

A failure here means `current` does not move. That is the whole point of having
publish be a separate stage.

---

### 10 · `publish` — make it live

**In:** validated artifacts · **Out:** `graphs/<date>/`, updated `graphs/current`

Move the artifacts into `graphs/<date>/`, write `manifest.json`, then flip
`graphs/current` atomically.

```json
{
  "graph_version": "20261001",
  "wiki": "enwiki",
  "dump_date": "2026-10-01",
  "built_at": "2026-10-14T09:22:11Z",
  "code_version": "git:554fdb9",
  "counts": {
    "articles": 7250570,
    "redirects": 11043928,
    "multi_hop_redirects": 8812,
    "index_keys": 18294498,
    "edges": 698341207,
    "mean_out_degree": 96.3
  },
  "dropped": {
    "red_links": 112843001,
    "dead_redirects": 48221,
    "self_links": 1904773,
    "redirect_sourced_links": 11043928
  },
  "files": {
    "forward.offsets": { "bytes": 29002284, "sha256": "..." },
    "forward.targets": { "bytes": 2793364828, "sha256": "..." }
  },
  "source_checksums": { "page.sql.gz": "sha1:...", "pagelinks.sql.gz": "sha1:..." }
}
```

On Windows, `graphs/current` is a directory junction (`mklink /J`) or, if
symlink creation is unavailable, a one-line `graphs/current.txt` naming the
directory. The API accepts either; a plain text pointer is still an atomic
replace.

---

## Testing

| Level | What |
|---|---|
| Unit | `sqldump.py` escape handling, DDL parsing, `normalize()` against `fixtures/normalization.jsonl` |
| Property (Hypothesis) | round-trip arbitrary titles through the MySQL quoter and the parser; redirect resolution on random chain/cycle graphs never loops and never invents a target |
| Fixture | hand-built miniature dumps (~20 pages) in `fixtures/tiny/`, exercising every drop rule, with the expected CSR committed byte-for-byte |
| Integration | full chain on `simplewiki`, asserted against golden fixtures |
| Acceptance | full chain on `enwiki`, stage 9 green |

The `fixtures/tiny/` dumps are worth the effort to write by hand: they make
stages 2–8 testable in milliseconds, which is what makes it reasonable to keep
changing them.

---

## Running it

The Typer CLI is the stage runner; there is no separate `make`/`just` layer, so
there is nothing to install beyond the project itself.

```bash
cd pipeline
uv sync                                                # one-time setup

uv run wikigraph --help                                # all ten stages
uv run wikigraph fetch --wiki simplewiki --date latest  # pins and prints a date
uv run wikigraph pages --wiki simplewiki --date 20261001
uv run wikigraph build --wiki simplewiki --date latest  # every built stage, in order

uv run pytest -q                                       # network tests deselected
uv run pytest -m network                               # the real dump host
uv run ruff check . && uv run ruff format --check .
```

Stages 3–10 are registered but exit 2 with a pointer to `docs/roadmap.md`.

Budget for enwiki: hours, dominated by stages 6 and 7. Disk: 50 GB+ free for
`raw/` plus `work/` plus DuckDB spill.
