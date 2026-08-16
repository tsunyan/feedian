## Specifications (`docs/specs/`)

For substantial changes, write a specification and reach agreement on it before implementation.

### File names

`YYYYMMdd-title.ja.md` (example: `20260816-ci-pipeline.ja.md`)

- The date is the **creation date**, not the finalization date. Once assigned, do not change it.
- The title must be English kebab-case. Do not use spaces, `/`, or `:`.
  As with writing commit summary lines in English, this makes file listings useful as an index.
- **Never rename a file once it has been named.** Renaming breaks links and makes its git history harder to follow.

### Structure

```markdown
# <タイトル>

ステータス: 草案 | レビュー中 | 確定

## 最終案

## 草案

## レビュー

### レビュー1 — Codex (2026-08-16)

### レビュー2 — Claude Code (2026-08-16)
```

- **The document body must be in Japanese.** Only the file name is in English.
- **Status (`ステータス`)** — Required. Determine the document's state from this line, not from the presence or absence of sections.
- **Final proposal (`最終案`)** — When finalizing, **insert this section before the draft**. Put it first because the conclusion is what readers need to see first.
  **A human writes this section. Agents must not write it until instructed to do so.**
- **Draft (`草案`)** — The initial specification proposal. Either a human or an agent may write it.
- **Review (`レビュー`)** — Zero or more rounds. Give each round a `### レビュー<N> — <名前> (YYYY-MM-DD)` heading.
  The name must be `Claude Code`, `Codex`, or a person's name.
  More importantly than the feedback itself, always record **whether it was accepted or rejected and why**. The reasons for rejecting proposals become valuable later.
- Do not include the word “specification” in the document title; its location under `docs/specs/` already makes that clear.
- If no review is needed, omit the `レビュー` and `最終案` sections and finalize the draft as-is.
  The status must still be `確定`.

### Relationship with DESIGN.md

Keep the roles separate to avoid maintaining the same information in two places.

| | Role | Updates |
|---|---|---|
| `docs/specs/` | Details and ADRs for individual specifications: why decisions were made and which alternatives were rejected | Do not edit after finalization |
| `DESIGN.md` | Summaries of finalized specifications and the overall design | Update during implementation and link to the relevant specification |

When in doubt, use `DESIGN.md` for “how things work now” and the specification for “why this decision was made.”
If `DESIGN.md` contains only a summary without a link to the specification, readers lose the path to the details.

### Lifecycle and commits

1. **Write the draft** — Do not commit it.
2. **Review it** — Zero or more rounds. Do not commit it.
3. **Finalize it** — A human inserts the `最終案` section before the draft and changes the status to `確定`.
   Only at this point, commit the specification **by itself with the `docs:` type**.
4. **Implement it** — Include both the code and the `DESIGN.md` summary update in the same commit, in accordance with the commit conventions.

Before finalization, **append rather than rewrite**. Only the status line may be rewritten.

- **Do not correct the draft even when errors are found.** Record feedback in the review section and the conclusion in the final proposal.
  Editing the draft makes feedback that refers to it impossible to understand. Its value as an ADR comes precisely from preserving rejected proposals.
- Typographical, formatting, and other corrections that do not change meaning may be made at any time.

Specifications are outside git before finalization, which creates two concerns.

- **Do not accidentally include them with `git add -A`.** An unfinalized specification could end up in an unrelated commit.
- **There is no backup.** Review discussions cannot be recovered. If the process is likely to take a long time, finalize at a sensible boundary and continue in a separate specification.

For a specification written after implementation as a historical record, **state clearly at the beginning that it was written retrospectively**.
Otherwise, the dates in the review headings will not reflect the actual chronology.

<!-- graft:start -->
## Graft — repo context graph

This repo is indexed in `graft/`: small linked markdown nodes that explain each
system and carry exact file:line spans, kept in sync with the code through git.

For ANY task here — understanding how something works, finding where code lives,
or scoping a change — get context from the graph before grepping or opening
source files. Re-ask freely (it's cheap) and reuse literal identifiers you
already have (symbol, error string, file name) as the query. New to this repo?
Run `graft map` first — a token-budgeted orientation (dir clusters, hubs,
hotspots), no LLM, no key.

- Run `graft ask "<your question>" --source` → ranked nodes with the relevant
  code spans inlined (each hit's ≤8-line crux by default; `--full` for whole
  definitions when the crux isn't enough). Match the tool to the task shape:
  for understanding or editing, the top node IS the answer — cite its
  `covers:` file:line spans and edit straight from `--source`. For
  exhaustive tasks ("every occurrence / every caller of this pattern"), ranked
  results are top-N, not complete — run `graft grep "<literal>"` instead
  (exhaustive over indexed files, grouped by enclosing symbol), falling back
  to raw `grep -rn` only for unindexed files.
- `graft skeleton <file>` → every definition's signature + span, ~10× cheaper
  than reading the file; use it to skim an API surface.
- `graft callers <symbol>` gives precomputed, exact edges — who calls this.
  Add `--direction out` for what it calls, or `--depth N` to walk
  transitively for the full blast radius. For structural questions, skip
  ranking and use this directly.
- Or browse: `graft/INDEX.md` lists every node; follow the links.
- Monorepos and folders of multiple repos rank fairly across sub-projects —
  hits carry `[scope/]` labels naming which one they're from. Narrow with
  `graft ask "<task>" --in <scope>/` once you know where you're working.

If a returned span is truncated ("+N more lines"), open the file at that exact
range before finalizing. Only open source files when a node genuinely lacks a
needed detail, and then at the exact file:line the node points to — never
re-read whole files.

After big code changes, refresh the graph with `graft build` (deterministic,
no API key, $0).
<!-- graft:end -->
