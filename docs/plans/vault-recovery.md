# Vault Write Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop before an OpenAI request when the vault is unavailable and recover the one completed LLM result that could not be written to the vault.

**Architecture:** A new recovery module persists one local transaction per output directory under `~/.raindian/pending`. `process_bookmarks` validates the usage log before every LLM request, saves a pending transaction after receiving the response, writes the Markdown and usage record to the vault, and deletes the transaction only after both succeed.

**Tech Stack:** Python standard library and the existing `unittest` suite.

## Global Constraints

- Store pending data outside the configured vault and retain at most one transaction per output directory.
- Do not make another OpenAI request after an availability, Markdown, usage, or local-cleanup failure.
- Preserve historical usage JSONL lines; add a transaction ID only to new summary records.
- Do not change `--no-llm`, `--dry-run`, estimate, or Raindrop sync behavior.

---

### Task 1: Add local pending-transaction primitives

**Files:**
- Create: `raindian/recovery.py`
- Test: `tests/test_recovery.py`

**Interfaces:**
- `PendingTransaction(transaction_id: str, target: Path, markdown: str, usage_record: dict[str, Any])`
- `pending_path(destination: Path, state_root: Path | None = None) -> Path`
- `save_pending`, `load_pending`, and `remove_pending`

- [ ] **Step 1: Write failing round-trip and destination-isolation tests**

```python
transaction = PendingTransaction("txn-1", destination / "one.md", "# One", {"transaction_id": "txn-1"})
save_pending(destination, transaction, state_root=state_root)
self.assertEqual(load_pending(destination, state_root=state_root), transaction)
self.assertIsNone(load_pending(other_destination, state_root=state_root))
```

- [ ] **Step 2: Implement atomic local JSON persistence**

```python
def pending_path(destination: Path, state_root: Path | None = None) -> Path:
    root = state_root or (Path.home() / ".raindian" / "pending")
    digest = hashlib.sha256(str(destination.resolve()).encode("utf-8")).hexdigest()
    return root / f"{digest}.json"
```

Write to a sibling temporary file and replace the final file after the JSON is fully written. Reject malformed pending state with `ValueError`.

- [ ] **Step 3: Verify and commit**

Run `python -m unittest tests.test_recovery -v`, then commit `feat: persist pending vault writes locally`.

### Task 2: Make usage records idempotent

**Files:**
- Modify: `raindian/__main__.py:248-278`
- Test: `tests/test_main.py`

**Interfaces:**
- `build_usage_record(...) -> dict[str, Any]` creates a UUID `transaction_id`.
- `append_usage_record(destination: Path, record: dict[str, Any]) -> None`.
- `usage_record_exists(destination: Path, transaction_id: str) -> bool`.

- [ ] **Step 1: Write a failing transaction lookup test**

```python
usage_path.write_text('{"transaction_id":"txn-a"}\n', encoding="utf-8")
self.assertTrue(usage_record_exists(destination, "txn-a"))
self.assertFalse(usage_record_exists(destination, "txn-b"))
```

- [ ] **Step 2: Split record construction from append**

Implement line-by-line JSONL scanning that skips malformed existing lines, keep all current price and token fields, and add `transaction_id` to new records.

- [ ] **Step 3: Verify and commit**

Run `python -m unittest tests.test_main -v`, then commit `feat: make usage records recoverable`.

### Task 3: Guard LLM calls and recover pending writes

**Files:**
- Modify: `raindian/__main__.py:319-490`
- Test: `tests/test_main.py`

**Interfaces:**
- `ensure_usage_log_readable(destination: Path) -> None`
- `recover_pending_transaction(destination: Path) -> None`

- [ ] **Step 1: Add failing process tests**

```python
with patch("raindian.__main__.ensure_usage_log_readable", side_effect=OSError("drive offline")), \
     patch("raindian.__main__.summarize_bookmark") as summarize:
    self.assertEqual(process_bookmarks(config, args), 1)
summarize.assert_not_called()
```

Also test a Markdown-write failure with two source items: exactly one summary call occurs and one pending transaction remains. Add recovery tests for a missing note, an already-written note, and an already-appended usage line.

- [ ] **Step 2: Implement the transaction sequence**

```python
ensure_usage_log_readable(destination)
summary = summarize_bookmark(...)
save_pending(destination, transaction)
write_note_atomically(transaction.target, transaction.markdown)
append_usage_record(destination, transaction.usage_record)
remove_pending(destination)
```

At the start of a non-dry-run LLM command, recover an existing transaction before iterating bookmarks. Write the note only when its target is missing; append usage only when the transaction ID is absent. Retain the local transaction and exit nonzero on any recovery or vault-write error.

- [ ] **Step 3: Verify and commit**

Run `python -m unittest tests.test_main tests.test_recovery -v`, then commit `feat: stop safely when vault writes fail`.

### Task 4: Document and run full verification

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Document the failure behavior**

State that Raindian stops before further OpenAI requests, keeps one completed item under `~/.raindian/pending`, and flushes it automatically on the next LLM run.

- [ ] **Step 2: Run final verification and commit**

Run `git diff --check && python -m unittest discover -s tests -v`. Commit the documentation with `docs: explain vault write recovery`.
