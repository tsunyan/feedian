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
| [GPT-5.6 Terra](https://developers.openai.com/api/docs/models/gpt-5.6-terra) | $2.50 / $15.00 | $7.63-$16.61 |
| [GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna) | $1.00 / $6.00 | $3.05-$6.65 |
| [GPT-5.5](https://developers.openai.com/api/docs/models/gpt-5.5) | $5.00 / $30.00 | $15.27-$33.23 |

Costs scale approximately linearly with the bookmark count. For `N` bookmarks, multiply the 449-bookmark estimate by `N / 449`.

The estimate is intentionally broad. `max_article_chars` limits fetched page text by characters, not API tokens. `max_output_tokens` is a hard ceiling for visible output and reasoning tokens, but actual input length still varies with page language and metadata. Check the linked official OpenAI documentation before a large run because model pricing can change.

## Sampled Cost Estimate

Use `--estimate` to calculate a project-specific estimate without calling the OpenAI API or writing notes. It needs `RAINDROP_TOKEN`, fetches a representative sample of linked pages, builds the same prompts used for normal processing, and counts their input tokens locally with `tiktoken`.

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

The output always shows GPT-5.6 Sol, GPT-5.6 Terra, GPT-5.6 Luna, and GPT-5.5. The `openai_model` from your configuration, or its `OPENAI_MODEL` override, is labeled `selected`; the `gpt-5.6` alias maps to Sol, while other models outside the comparison are named explicitly. A failed page fetch still estimates the fallback prompt made from Raindrop metadata and the fetch error. The estimate uses current uncached token prices and the configured `max_output_tokens` as a per-bookmark output ceiling; server-side request framing, reasoning tokens, or future price changes can still make the final bill differ.

## Behavior

- Raindrop.io is read-only. This tool does not edit bookmark tags in Raindrop.
- Existing notes are skipped unless `--force` is passed.
- Filenames include the Raindrop item ID to avoid collisions.
- If a web page cannot be fetched, the tool still uses Raindrop metadata such as title and excerpt.
- HTML extraction prioritizes `article` and `main`, then article-like `class` / `id` values. Navigation, headers, footers, sidebars, ads, related links, comments, and cookie banners are excluded when identifiable from HTML structure or attributes.
- Page fetching accepts only `http` and `https` URLs and rejects local/private network addresses by default, including after redirects. Set `allow_private_urls` only for a trusted internal bookmark collection.
- Linked-page text and bookmark metadata are treated as untrusted reference data when sent to the LLM; instructions in them are not followed.
- Raindrop and OpenAI requests retry transient 408, 409, 425, 429, 5xx, and network failures with bounded exponential backoff.

## Useful Commands

List collections:

```powershell
python -m raindian --config config.json --list-collections
```

Process one collection:

```powershell
python -m raindian --config config.json --collection 123456 --limit 20
```

Use a different vault without editing config:

```powershell
python -m raindian --config config.json --vault "C:\Users\you\Documents\Obsidian\Vault"
```

## License

This project is licensed under the [MIT License](LICENSE).
