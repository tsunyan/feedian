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

## Code reviews (`docs/reviews/`)

Write a review document when the review covers the implementation of a specification, or when at least one
finding is rejected or deferred. A review whose findings were all fixed on the spot needs no document —
git history already records that.

### File names

`YYYYMMdd-title.ja.md`, following the specification rules above. The date is the review date.
**Never rename a file once it has been named.**

### Structure

```markdown
# <タイトル>のコードレビュー

ステータス: 指摘中 | 対応中 | 完了
対象: <レビュー対象のコミット>（このコミットの親）
仕様: <関連する docs/specs/ へのリンク。無ければ省略>
レビュー者: Claude Code (2026-08-16)

## 結論

## 指摘

### 1. <一行の主張> — 重大度: 高 | 中 | 低

## 採否

## 検証

## 規約化した項目
```

- **The document body must be in Japanese.** Only the file name is in English.
- **Target (`対象`)** — Required. Name the reviewed commit by hash and summary. Because the document is
  committed together with its fixes, that commit is normally the parent of the commit carrying the document.
- **Cite the finalized specification.** When one exists, quote `docs/specs/YYYYMMdd-*.ja.md` as the evidence
  for what the implementation owes. Never argue from a pre-finalization draft: the draft is what was proposed,
  not what was decided, and a finding built on it inverts the review.
- **Findings (`指摘`)** — Number them. The number is the finding ID; refer to it from fix commits as
  `<文書の日付>-<番号>` (example: `20260816-2`). Every finding states its evidence as `file:line`, what actually
  happens, and what it costs. A finding nobody can reproduce is not a finding.
- **Disposition (`採否`)** — Required, one row per finding, using 採用 / 修正して採用 / 不採用 / 保留 as the
  specification review sections do. A fixed finding compresses to one line naming the commit.
  **A rejected or deferred finding carries its reasoning** — that is what this document exists for.
- **Promoted rules (`規約化した項目`)** — When the same finding appears in a second review, promote it to a rule
  in this file and record the promotion here. A rule an agent reads is worth more than a finding repeated.

### Lifecycle and commits

1. **Write the findings** — Do not commit it.
2. **Apply the fixes** — Squash them into one commit so that `対象` stays exactly one commit behind.
3. **Complete it** — Fill in `採否` and `検証`, set the status to `完了`, and commit the document
   **in the same commit as the fixes**.

- Reviews are published. Never place `docs/reviews/` in `.gitignore`.
- When a review changes no code, commit the document by itself with the `docs:` type.
  A review that rejects every finding still belongs in the repository; its reasoning is the valuable part.
- When the fixes will take more than a day, commit the document early with the status `対応中`
  rather than leaving it outside git.
- **Do not accidentally include an in-progress review with `git add -A`** — the same hazard specifications carry.
- A finalized specification is never edited. When an implementation deviates from a finalized specification,
  **the review document is where that deviation and its resolution are recorded.**

## Branch names

`<type>/<kebab-case-noun-phrase>` (examples: `feat/llm-backends`, `fix/manus-status`, `ci/security-hardening`)

- The type comes from the same set the commit summary line uses.
- **Use a noun phrase, not a verb phrase.** `feat/llm-backends`, not `feat/implement-llm-backends`.
  The branch names what the work is about; its commits say what it does.
- **Do not prefix a branch with the name of the agent or person who created it.** Git already records the
  author, and a branch normally outlives whoever opened it — a review and its fixes often land on the branch
  that first carried the implementation.
- Branches Dependabot opens are outside this convention.

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
