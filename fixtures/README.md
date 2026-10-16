# Fixtures

Small committed inputs that make the pipeline and the API testable without
touching a real dump.

| Path | Used by | Contents |
|---|---|---|
| `tiny/` | `pipeline` | Hand-built miniature dumps, one per table, as **plain SQL** |
| `normalization.jsonl` | `pipeline` + `api` | Title-normalization vectors — the authority for both implementations *(not written yet)* |
| `golden/` | `pipeline` stage 9 | Known links and known paths that must hold on the real wiki *(not written yet)* |

## `tiny/`

Committed as plain `.sql` so they stay readable and diffable; the test suite
gzips them into `tmp_path` on the way in, because the real dumps are gzipped and
the parser should be exercised the same way.

They are deliberately nasty. `page.sql` carries titles containing `)`, `,`,
`;`, escaped quotes, a trailing backslash, a doubled `''`, `\%` (which keeps its
backslash in MySQL), and non-ASCII — plus a `KEY` named after a column, to prove
the DDL reader distinguishes column definitions from index definitions. Each of
those has broken a naive dump parser at some point.

Keep them tiny. Their value is that stages 2–8 become testable in milliseconds,
which is what makes it reasonable to keep changing those stages.

## `normalization.jsonl`

When it exists, this file is the **authority** for `normalize()`, which is
implemented twice — Python in the pipeline, Rust in the API. The published title
index is sorted by the Python version's output and searched with the Rust
version's, so a disagreement is a silent wrong answer rather than a crash. Add a
case here *before* changing either implementation.
