# Raindian

![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)
![Dependencies](https://img.shields.io/badge/dependencies-tiktoken-4C8CBF)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

Raindrop.io bookmarks to Obsidian Markdown notes.

`Raindian` (Raindrop -> Obsidian) reads bookmarks from the Raindrop.io REST API, fetches each linked page, optionally asks the OpenAI Responses API for a Japanese summary and tags, and writes one `.md` file per bookmark into a local Obsidian vault.

## Requirements

- Python 3.11+
- A Raindrop.io access token
- An OpenAI API key for AI summaries and tags (optional with `--no-llm`, `--dry-run`, or `--estimate`)
- A local Obsidian vault folder

Install the required tokenizer:

```powershell
python -m pip install -r requirements.txt
```

For personal use, Raindrop.io lets you copy a test token from your application settings in the App Management Console. Use that value as `RAINDROP_TOKEN`.

## Setup

Copy the example config and edit the vault path:

```powershell
Copy-Item config.example.json config.json
notepad config.json
```

Set the Raindrop token in your shell:

```powershell
$env:RAINDROP_TOKEN = "your-raindrop-token"
```

For AI summaries and tags, also set an OpenAI API key:

```powershell
$env:OPENAI_API_KEY = "your-openai-api-key"
```

Or copy `.env.example` to `.env` and edit it. Omit `OPENAI_API_KEY` when using `--no-llm`, `--dry-run`, or `--estimate`. `.env` is ignored by Git.

Run a dry run first:

```powershell
python -m raindian --config config.json --limit 3 --dry-run
```

Dry runs query Raindrop to list the target bookmarks, but do not fetch linked pages, call OpenAI, or write files. An OpenAI API key is not required for a dry run.

Create notes:

```powershell
python -m raindian --config config.json --limit 10
```

## Config

See `config.example.json`.

Important fields:

- `vault_path`: Absolute path to your Obsidian vault.
- `output_folder`: Folder inside the vault where notes are written.
- `collection_id`: Raindrop collection ID. Use `0` for all bookmarks.
- `nested`: Include nested collections when reading a collection.
- `base_tags`: Tags added to every generated note.
- `openai_model`: Model used when AI summaries are enabled. Override with `OPENAI_MODEL` if your account uses a different model.
- `max_article_chars`: Maximum linked-page text sent to OpenAI (default: `10000`).
- `max_output_tokens`: Hard upper bound for visible output and reasoning tokens per OpenAI request (default: `800`).
- `openai_reasoning_effort`: Reasoning effort for supported OpenAI models (default: `none`). Use `low` only when testing shows it improves note quality.
- `allow_private_urls`: Allow page fetches to private or local network addresses (default: `false`).
- `max_retries`: Maximum retries after a transient Raindrop or OpenAI API failure (default: `3`).
- `retry_base_seconds`: Initial retry delay; delays double on each retry (default: `1.0`).
- `sync_request_interval_seconds`: Minimum interval between Raindrop HTTP requests during note or tag sync only (default: `0.5`).

## OpenAI API Cost Estimate

`--no-llm` does not call the OpenAI API, so it does not incur OpenAI API charges.

The following rough estimate uses standard, uncached text pricing as of 2026-08-09 and assumes:

- 449 bookmarks
- About 2,000-10,000 input tokens per bookmark (up to 10,000 fetched page characters plus metadata)
- At most 800 output tokens per bookmark, including reasoning tokens
- One Responses API request per bookmark
- No Batch API, prompt caching, or regional-processing surcharge

| Model | Input / output per 1M tokens | Estimated total for 449 bookmarks |
| --- | ---: | ---: |
| [GPT-5.6 Sol](https://developers.openai.com/api/docs/models/gpt-5.6-sol) | $5.00 / $30.00 | $15.27-$33.23 |
| [GPT-5.6 Terra](https://developers.openai.com/api/docs/models/gpt-5.6-terra) | $2.00 / $12.00 | $6.11-$13.29 |
| [GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna) | $0.20 / $1.20 | $0.61-$1.33 |
| [GPT-5.5](https://developers.openai.com/api/docs/models/gpt-5.5) | $5.00 / $30.00 | $15.27-$33.23 |

Costs scale approximately linearly with the bookmark count. For `N` bookmarks, multiply the 449-bookmark estimate by `N / 449`.

The estimate is intentionally broad. `max_article_chars` limits fetched page text by characters, not API tokens. `max_output_tokens` is a hard ceiling for visible output and reasoning tokens, but actual input length still varies with page language and metadata. Check the linked official OpenAI documentation before a large run because model pricing can change.

## Sampled Cost Estimate

Use `--estimate` to calculate a project-specific estimate without calling an OpenAI model API or writing notes. It needs `RAINDROP_TOKEN`, fetches a representative sample of linked pages, builds the same prompts used for normal processing, and counts their input tokens locally with `tiktoken`. It also reads the public OpenAI model documentation to refresh prices; this does not require `OPENAI_API_KEY` or incur model API charges.

```powershell
python -m raindian --config config.json --estimate
```

The default sample size is `10%`, with a minimum of 20 pages when the target has at least 20 bookmarks. Samples are proportional to Raindrop collections and evenly spaced within each collection's API order.

```powershell
# Use exactly 50 sampled pages.
python -m raindian --config config.json --estimate --estimate-sample-size 50

# Use 20% of the target bookmarks (at least 20 pages).
python -m raindian --config config.json --estimate --estimate-sample-size 20%

# Do not fetch pages; use the generic 2,000-10,000 input-token range instead.
python -m raindian --config config.json --estimate --estimate-sample-size 0

# Estimate metadata and excerpts only, without fetching linked pages.
python -m raindian --config config.json --estimate --skip-page-fetch
```

The command reports its current phase while it runs: official price refresh, bookmark collection, every 50 collected bookmarks, sample selection, each page fetch, and cost calculation. On each run it reads the [official OpenAI model catalog](https://developers.openai.com/api/docs/models) and prices for its recommended models, then also includes the configured `openai_model` (or `OPENAI_MODEL` override) and labels it `selected`. The `gpt-5.6` alias maps to Sol. If the catalog cannot be read or parsed, it prints `price_source=fallback` and uses the built-in price table instead. When pages are sampled, the table shows both a typical and a maximum estimate. The typical estimate uses the aggregate `output_tokens / input_tokens` ratio from matching usage records when available; otherwise it uses the initial `input-matched` assumption that output tokens equal the measured mean input tokens. The maximum estimate uses `max_output_tokens`. A failed page fetch still estimates the fallback prompt made from Raindrop metadata and the fetch error. Server-side request framing, reasoning tokens, or future price changes can still make the final bill differ.

## Behavior

- Normal note generation is read-only against Raindrop.io. `--sync-raindrop-summary` and `--sync-raindrop-tags` are explicit opt-in operations that write Raindrop notes or tags.
- Existing LLM-generated notes are skipped unless `--force` is passed. An LLM run automatically upgrades notes that were previously created with `--no-llm`; a later `--no-llm` run never downgrades an LLM-generated note.
- New LLM-generated filenames use the Japanese note title and include the Raindrop item ID to avoid collisions. Use `--rename-existing` to rename existing LLM notes from their stored note titles and to rename `--no-llm` notes when they are upgraded.
- Notes preserve the original Raindrop title and excerpt, and store the cleaned linked-page text as `Extracted Content (Original)`. Existing notes are not backfilled automatically.
- If a web page cannot be fetched, the tool still uses Raindrop metadata such as title and excerpt.
- HTML extraction prioritizes `article` and `main`, then article-like `class` / `id` values. Navigation, headers, footers, sidebars, ads, related links, comments, and cookie banners are excluded when identifiable from HTML structure or attributes.
- Page fetching accepts only `http` and `https` URLs and rejects local/private network addresses by default, including after redirects. Set `allow_private_urls` only for a trusted internal bookmark collection.
- Linked-page text and bookmark metadata are treated as untrusted reference data when sent to the LLM; instructions in them are not followed.
- Raindrop and OpenAI requests retry transient 408, 409, 425, 429, 5xx, and network failures with bounded exponential backoff.
- Raindrop note and tag syncs pace each HTTP request independently at `sync_request_interval_seconds`. On a 429 response, the retry waits for the later of `Retry-After` and Raindrop's `X-RateLimit-Reset` time before retrying. Each sync prints its request interval at start and `elapsed=<seconds>s` when complete.
- Before each OpenAI summary request, Raindian reads the usage log to confirm the vault is available. If the vault cannot be read or a Markdown or usage write fails, it stops before making further OpenAI requests.
- After an LLM response, Raindian temporarily stores at most one pending note under `~/.raindian/pending` until both the Markdown note and usage record are stored in the vault. The next LLM run automatically completes this pending write without requesting another summary; the local pending file is then removed.
- Successful LLM summaries append a JSON line with `operation: "summarize"` and a transaction ID to `<vault_path>/<output_folder>/.raindian-usage.jsonl`. Each line contains token usage, model and reasoning settings, a price snapshot, and the request's estimated USD cost; it does not contain page text or URLs.

## Useful Commands

List collections:

```powershell
python -m raindian --config config.json --list-collections
```

Process one collection:

```powershell
python -m raindian --config config.json --collection 123456 --limit 20
```

Upgrade `--no-llm` notes with LLM summaries and rename notes to their Japanese titles. Existing LLM notes are renamed from their saved frontmatter title without another OpenAI call:

```powershell
python -m raindian --config config.json --rename-existing
```

Preview copying existing Japanese LLM summaries into Raindrop notes. This reads local Markdown only and does not call OpenAI or modify Raindrop:

```powershell
python -m raindian --config config.json --sync-raindrop-summary --dry-run
```

Apply the summary sync. Raindian appends or updates only its managed `Raindian Summary` block in each Raindrop note, preserving any manual note text:

```powershell
python -m raindian --config config.json --sync-raindrop-summary
```

Preview tags from existing LLM notes before adding them to the matching Raindrop items:

```powershell
python -m raindian --config config.json --sync-raindrop-tags --dry-run
```

Apply the tag sync. Raindian excludes `base_tags`, reads the item's current Raindrop tags, and appends only missing tags. It never replaces or removes existing Raindrop tags:

```powershell
python -m raindian --config config.json --sync-raindrop-tags
```

Use a different vault without editing config:

```powershell
python -m raindian --config config.json --vault "C:\Users\you\Documents\Obsidian\Vault"
```

## License

This project is licensed under the [MIT License](LICENSE).
