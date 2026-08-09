# Vault Write Recovery

## Goal

Avoid additional OpenAI requests when the configured Obsidian vault becomes
unavailable, while preserving the one completed response that may be in memory
when the outage occurs.

## Normal flow

Before every OpenAI summary request, Raindian reads the usage log in the output
folder. A read failure is treated as an unavailable vault and stops the command
before the request is made.

After a successful OpenAI response, Raindian creates a local, atomic pending
record containing the rendered Markdown, the usage record, the destination
path, and a transaction identifier. It then writes the note to the vault and
appends the usage record. Once both succeed, it removes the pending record.

## Failure and resume

Any Markdown write failure or usage-log write failure stops the command. The
pending record remains local; it contains no fetched page text beyond the
rendered note and does not accumulate during successful processing.

On the next non-dry-run command, Raindian first attempts to complete the
pending record. The usage record carries a transaction identifier, so recovery
does not append it twice if a prior append succeeded just before interruption.
Only after recovery succeeds does normal bookmark processing continue.

## Scope

- Applies to LLM summary generation only.
- `--no-llm`, `--dry-run`, estimate, and Raindrop sync commands do not create
  or recover pending records.
- The pending directory is local to the machine, not inside the Google Drive
  vault. At most one record is kept because processing stops on the first
  vault-write failure.
- Existing usage-log lines without a transaction identifier remain valid.

## Verification

Tests cover: the pre-request availability check, failure after an LLM response,
resume without another OpenAI call, and no duplicate usage line after recovery.
