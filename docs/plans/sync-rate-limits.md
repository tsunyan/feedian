# Raindrop Sync Rate Limits Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pace only Raindrop sync requests at 0.5 seconds and wait for the server reset time after HTTP 429.

**Architecture:** `Config` exposes a sync-only interval. `RaindropClient` enforces that interval immediately before each HTTP attempt. The shared retry helper calculates a 429 delay from `Retry-After` and the Raindrop `X-RateLimit-Reset` epoch header. Sync startup prints the active interval; existing final elapsed output remains unchanged.

**Tech Stack:** Python standard library and `unittest`.

## Global Constraints

- Default `sync_request_interval_seconds` is `0.5`.
- Normal bookmark generation and estimate requests remain unthrottled by this new setting.
- A 429 waits until the latest advertised retry/reset time plus a small safety margin.
- Both sync final summaries retain `elapsed=<seconds>s`.

---

### Task 1: Add configuration and a per-client request interval

**Files:**
- Modify: `raindian/config.py`, `config.example.json`
- Modify: `raindian/raindrop.py`
- Test: `tests/test_config.py`, `tests/test_raindrop.py`

- [ ] Write failing tests for the `0.5` default and two client requests separated by the configured interval.
- [ ] Add `sync_request_interval_seconds` validation and pass it only to sync-created `RaindropClient` instances.
- [ ] Add a monotonic-clock request gate before every `urlopen` call; zero leaves existing behavior unchanged.
- [ ] Run focused config/client tests and commit `feat: throttle Raindrop sync requests`.

### Task 2: Honor Raindrop reset headers on 429

**Files:**
- Modify: `raindian/retry.py`
- Test: `tests/test_retry.py`

- [ ] Write a failing 429 test with `X-RateLimit-Reset` later than `Retry-After`.
- [ ] Compute a delay from UTC epoch seconds using wall-clock time and add a small safety margin; keep exponential backoff for responses without those headers.
- [ ] Run retry tests and commit `fix: wait for Raindrop rate limit resets`.

### Task 3: Surface sync pacing and document it

**Files:**
- Modify: `raindian/__main__.py`, `README.md`
- Test: `tests/test_main.py`

- [ ] Write failing tests asserting both sync commands print `request_interval=0.5s` and still print `elapsed=`.
- [ ] Print the interval at sync startup and describe the setting, 429 behavior, and elapsed output in the README.
- [ ] Run `git diff --check && python -m unittest discover -s tests -v` and commit `docs: explain Raindrop sync throttling`.
