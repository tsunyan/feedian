# Estimate Command Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only `--estimate` command that samples representative bookmark pages and projects costs for all supported OpenAI models.

**Architecture:** Keep CLI orchestration in `raindian/__main__.py`; add `raindian/estimate.py` for pure sample parsing, stratified selection, token counting, pricing, and output rendering. Estimate mode reuses normal page extraction and prompt construction without calling OpenAI.

**Tech Stack:** Python 3.11+, standard library, `tiktoken`, unittest, Raindrop REST client.

## Global Constraints

- Estimate mode must not call OpenAI, write notes, or require `OPENAI_API_KEY`.
- Default is `10%`, minimum 20 for populations of at least 20; `SIZE` accepts an integer, percentage, or `0`.
- Sampling is proportional by collection and evenly spaced in each collection's API order.
- Output always lists Sol, Terra, Luna, and GPT-5.5 and marks the configured model `selected`.
- Sampled fetches use normal safety, extraction, timeout, and retry settings.

---

## File Structure

- Create `raindian/estimate.py`: pure sample parsing, allocation, token counting, projections, formatting.
- Modify `raindian/__main__.py`: options, validation, and estimate route.
- Create `tests/test_estimate.py`: unit plus mocked route coverage.
- Modify `tests/test_main.py`: option-conflict coverage.
- Create `requirements.txt`: pinned `tiktoken` runtime dependency.
- Modify `README.md`: installation and use documentation.

### Task 1: Estimation Primitives

**Files:** `raindian/estimate.py`, `tests/test_estimate.py`

**Interfaces:** `parse_sample_size(value: str, population: int) -> int`; `select_sample(items: list[dict[str, Any]], sample_size: int) -> list[dict[str, Any]]`; `count_prompt_tokens(prompt: str, model: str) -> tuple[int, str | None]`; `projected_costs(population: int, input_tokens_per_item: float, max_output_tokens: int) -> list[CostRow]`.

- [x] **Step 1: Write failing tests.** Cover `10%` resolving to 20 for 50 items and 312 for 3,112; `0`; invalid values; population clamping; proportional allocation; deterministic midpoint selection; all four models; and unknown-model fallback to `o200k_base`.
- [x] **Step 2: Run `python -m unittest tests.test_estimate -v`.** Confirmed the expected import failure because the module did not exist.
- [x] **Step 3: Implement the minimum pure functions.** Parse percentage/integer forms; group by `collection.$id`; use largest-remainder allocation and midpoint indices; use `tiktoken.encoding_for_model` with `o200k_base` fallback; project sampled mean input across the population and add `population * max_output_tokens` as output ceiling.
- [x] **Step 4: Re-run `python -m unittest tests.test_estimate -v`.** PASS.
- [x] **Step 5: Commit.** Committed as `19764e4 feat: add cost estimate primitives`.

### Task 2: Read-Only CLI Flow

**Files:** `raindian/__main__.py`, `tests/test_main.py`, `tests/test_estimate.py`

**Interfaces:** New `estimate_bookmarks(config: Config, args: argparse.Namespace) -> int` consumes Task 1 functions.

- [x] **Step 1: Write failing tests.** Mock a Raindrop item and successful page, set only `RAINDROP_TOKEN`, run `main(["--config", str(config_path), "--estimate", "--estimate-sample-size", "1"])`, and assert no vault directory. Also cover conflicts with `--dry-run` and `--list-collections`, zero targets, all fetches failing, and `0` without page fetches.
- [x] **Step 2: Run `python -m unittest tests.test_main tests.test_estimate -v`.** Confirmed the expected missing `estimate_bookmarks` import failure.
- [x] **Step 3: Implement route.** Add `--estimate` and `--estimate-sample-size` with default `10%`; reject incompatible flags; require only `RAINDROP_TOKEN`; get the complete filtered population; route before `process_bookmarks` so it cannot create the output directory.
- [x] **Step 4: Implement measurement and output.** Fetch selected pages, build actual prompts with `build_prompt`, count locally, aggregate failures, and respect `sleep_seconds`. Print target/sample/success/failure counts, mean and projected tokens, output cap, elapsed time, four-price table, selected marker, and tokenizer fallback. Count-only mode labels its generic README-based range; failed fetches retain the normal metadata/error fallback prompt.
- [x] **Step 5: Re-run `python -m unittest tests.test_main tests.test_estimate -v`.** PASS.
- [x] **Step 6: Commit.** Committed as `face103 feat: add read-only estimate command`.

### Task 3: Dependency and Documentation

**Files:** `requirements.txt`, `README.md`

- [x] **Step 1: Add `tiktoken` as a pinned Python 3.11-compatible requirement.** Replaced standard-library-only badge/copy and documented `pip install -r requirements.txt`.
- [x] **Step 2: Document estimate commands.** Included default, absolute, percentage, and count-only forms; documented `RAINDROP_TOKEN`, sampled pages, and billing limitations.
- [x] **Step 3: Verify.** `python -m unittest discover -s tests -v`, `python -m raindian --help`, and `git diff --check` passed.
- [x] **Step 4: Commit.** Committed as `c21fba9 docs: explain sampled cost estimates`.
