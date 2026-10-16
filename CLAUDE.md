# Working notes for Claude Code

Project-specific context and conventions. For *what* this project is and *why*
it is shaped this way, read [`ARCHITECTURE.md`](ARCHITECTURE.md) and
[`docs/decisions.md`](docs/decisions.md) — those are the source of truth and
this file does not restate them.

## Orientation

Three subprojects, three languages, separated by files and HTTP rather than by
function calls:

| Dir | Language | Package | Role |
|---|---|---|---|
| `pipeline/` | Python 3.12 | `wikigraph` | dumps → graph artifacts |
| `api/` | Rust | `wiki-paths` | graph artifacts → HTTP |
| `bot/` | Python 3.12 | `wikibot` | harness, policies, evaluation |

`data/` (dumps, intermediates) and `graphs/` (published builds) are gitignored
and can be tens of gigabytes. Never commit anything from either, and never
delete from either without asking — an enwiki build is hours of work.

## Invariants that are not style preferences

Breaking any of these is a correctness bug, not a lint failure. They are
enforced by pipeline stage 9 and re-checked when the API loads a graph.

1. **No self-links in the edge set.** This is load-bearing: it is what makes
   every length-`d+1` walk a simple path, which is what the `d+1` top-up
   depends on. See
   [`docs/algorithms.md` Lemma 3](docs/algorithms.md#5--topping-up-with-length-d1-paths)
   and ADR-007. If you touch pipeline stage 6, re-read that proof first.
2. **A BFS layer is expanded completely before checking for a meeting.**
   Checking mid-layer can return a distance that is too large.
3. **`normalize()` must agree between Python and Rust.** The index is sorted by
   the Python version's output and searched with the Rust version's, so a
   disagreement is a silent wrong answer rather than a crash. Both are tested
   against `fixtures/normalization.jsonl`, which is the authority — add a case
   there *before* changing either implementation.
4. **Nothing `N`-sized is allocated or cleared per query.** `N ≈ 7.25M`. Scratch
   comes from a pooled, epoch-stamped allocation; the concurrency limit is what
   bounds the memory.
5. **Dense ids are not stable across builds.** Nothing outside a single graph
   version may persist one.
6. **DuckDB is a build tool only.** It must never appear in the serving path.

## Conventions

- **Pipeline stages are file-to-file and re-runnable.** A stage's output depends
  only on its inputs plus `(wiki, date)`. Delete the output, re-run, get the
  same *content* — not the same bytes, since Parquet embeds writer metadata, so
  don't assert on byte equality. Stages over the big tables checkpoint so a
  crash resumes.
- **Develop on `simplewiki`, test on `fixtures/tiny/`.** An enwiki run is hours;
  `simplewiki` is minutes; the hand-built fixtures are milliseconds. Do not
  reach for enwiki to debug something a fixture can show.
- **Measure before optimizing.** Several design notes say "measure first" and
  mean it — in particular, `sqldump.py` only gets rewritten in Rust once it is
  demonstrated to be the bottleneck (ADR-002).
- **No-path, a short sample, and a truncated top-up are successful answers**,
  returned as `200`. A hit search budget is an explicit error. Never return a
  plausible-looking distance that might be wrong.
- Tests: `pytest` + `hypothesis` on the Python sides, `cargo test` + `proptest`
  on the Rust side. The `N ≤ 12` brute-force path-set comparison in
  [`docs/algorithms.md §9`](docs/algorithms.md#9--testing) is the highest-value
  test in the project — it is the only thing that catches a wrong
  parent-recording condition.
- Lint/format: `ruff` for Python, `cargo fmt` + `cargo clippy` for Rust.
  `just check` runs everything.

## Environment

Development is on **Windows 11**, so prefer the PowerShell tool for shell work.
A few places where that matters:

- `graphs/current` is a directory junction (`mklink /J`) or, where symlink
  creation is unavailable, a one-line `graphs/current.txt`. The API accepts
  either.
- Write artifact files in binary mode; text-mode line-ending translation will
  silently corrupt a `u32` array.
- Keep paths out of hardcoded POSIX form; use `pathlib` and Rust's `Path`.

## Keeping the docs honest

The docs were written before the code, so parts of them are predictions:

- The sizing table in [`ARCHITECTURE.md §6`](ARCHITECTURE.md#sizing--english-wikipedia)
  is **estimates**. When a real build lands, replace them with the measured
  numbers from `manifest.json`.
- Every default in the config and budget tables is a starting guess. Replace
  with measured values as they are established.
- Design changes belong in [`docs/decisions.md`](docs/decisions.md) as a new or
  superseded ADR — the record of what was rejected and why is the part that
  decays fastest and is hardest to reconstruct later.
