<!--
This file is the entry point for Claude Code. The actual instructions are stored in `AGENTS.md` and imported from here, since Claude Code does not read `AGENTS.md` directly.
Add only Claude Code-specific instructions below the import.
Reference: [https://code.claude.com/docs/en/memory](https://code.claude.com/docs/en/memory)
-->

@AGENTS.md

## Delegation

The session is the architect. It owns requirements, decomposition, interface design, specs,
routing, and verification — and it does not type implementation code. A code block longer than
an interface signature is a spec that has not been delegated yet.

| Lane | Model | Route here |
|---|---|---|
| `implementer` | Sonnet | Default. The spec determines the outcome: wiring, mechanical edits, straightforward features, test bodies, boilerplate. |
| `advisor` | Opus, read-only | Not an implementation lane. Commitment boundaries and the mandatory final review. |

Deciding rule: how much does the outcome depend on judgment the spec cannot capture? Little →
`implementer`. A lot, and mistakes are costly → keep it. A task the implementer fails once gets a
corrected spec; twice means it was misclassified — take it back.

**Every delegation prompt carries five parts**: objective, files (exact paths), interfaces,
constraints, verification command. Implementers share none of this conversation's context. A spec
you cannot finish writing means the decision is not made yet — that is architect work, not
something to hand a cheaper model.

**Consult `advisor`** before committing to an architecture, migration, or API shape, whenever the
same problem has resisted two distinct attempts, and **always once before reporting a deliverable
done**. Act on the verdict or surface the disagreement; never silently ignore it. The advisor runs
the architect's own model, so this is a fresh-eyes check, not an independent-model one —
cross-vendor review comes from the Codex and CodeRabbit passes in CI.

**Reports are claims, not evidence.** Read the diff and re-run the verification command before
accepting any lane's work. "Tests should pass" means the task is not done.

Stay with the architect regardless of cost: specifications and review documents under `docs/`
(judgment written in Japanese prose, not mechanical work), commit and branch operations, and any
edit smaller than the spec describing it.

Use `graft` before delegating exploration — it is cheaper than a subagent and returns exact
`file:line`. Delegate a search only when the graph genuinely does not answer it.
