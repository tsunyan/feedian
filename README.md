# Feedian

![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)
![Dependencies](https://img.shields.io/badge/dependencies-see%20requirements.txt-4C8CBF)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

Bookmarks and feeds to Obsidian Markdown notes.

Feedian reads items through source adapters, converts them to a canonical item, optionally asks the OpenAI Responses API for a Japanese summary and tags, and writes one `.md` file per item into a local Obsidian vault. Raindrop.io and Hatena Bookmark exports are currently supported.

## Requirements

- Python 3.11+
- A Raindrop.io access token when using the Raindrop adapter
- An OpenAI API key for AI summaries and tags (optional with `--no-llm`, `--dry-run`, or `--estimate`)
- A local Obsidian vault folder

Install the Python dependencies and the lightweight headless Chromium runtime:

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium --only-shell
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
python -m feedian --config config.json --limit 3 --dry-run
```

Dry runs query Raindrop to list the target bookmarks, but do not fetch linked pages, call OpenAI, or write files. An OpenAI API key is not required for a dry run.

Create notes:

```powershell
python -m feedian --config config.json --limit 10
```

## Hatena Bookmark

Get your Hatena API key as follows:

1. Sign in to Hatena and open the [posting email address settings](https://www.hatena.ne.jp/my/config/mail/upload).
2. Find the API key shown on that page. If the posting address is displayed in a form such as `API_KEY.HATENA_ID@...`, use only the `API_KEY` part. Hatena's [official WSSE authentication documentation](https://developer.hatena.ne.jp/ja/documents/auth/apis/wsse/) also describes where to find it.
3. Set your Hatena ID and API key in `.env`:

```dotenv
HATENA_ID=your-hatena-id
HATENA_API_KEY=your-hatena-api-key
```

The API key is not your Hatena account password. Do not paste it into issues or chat, and do not commit `.env`; this repository's `.gitignore` excludes it.

Then export all indexed bookmarks, including private bookmarks, and create notes:

```powershell
python -m feedian --config config.json --source hatena --dry-run
python -m feedian --config config.json --source hatena --no-llm
python -m feedian --config config.json --source hatena
```

Both normal mode and `--no-llm` fetch each bookmarked URL and store the full extracted page text under `Extracted Content (Original)`. `--no-llm` omits AI summaries, key points, and AI-generated tags; normal mode adds them to the same note. Use `--skip-page-fetch` only when you want Hatena metadata without the linked-page content. HTML downloads have a 10 MiB safety limit. `max_article_chars` limits only the text sent to OpenAI, not the text stored in Markdown. The same extraction pipeline is used by the Raindrop adapter.

For a staged quality check, increase `--limit` without `--force`. Existing notes are skipped, so each run adds only the newly included bookmarks:

```powershell
feedian --source hatena --no-llm --limit 100
feedian --source hatena --no-llm --limit 500
feedian --source hatena --no-llm --limit 1000
```

Feedian decodes HTML with HTTP and HTML charset declarations plus statistical detection, extracts the main article with Trafilatura, and uses headless Chromium when the first result is empty, corrupted, or appears to be a JavaScript shell. Notes record `fetch_method`, `extraction_method`, `content_encoding`, and `content_chars` in frontmatter for review.

For every source item with a URL, Feedian also reads public bookmark comments from Hatena's official entry-information API. Page-level replies (currently separated explicitly for Hatena Anonymous Diary) and public Hatena comments are written to a sibling `*.comments.md` note linked from the main note. Comment and reply text is preserved but is not sent to the summary LLM. Set `hatena_fetch_public_comments` to `false` to disable the extra API request per item.

Feedian uses Hatena's authenticated My Bookmark Full-text Search API. Because that API requires a search query, Feedian searches the `https` and `http` URL schemes separately, requests up to 100 results per page, and removes duplicate URLs. It then converts every result to a canonical item before writing it to `hatena_output_folder` (default: `Hatena`). Hatena notes preserve comments, tags encoded at the start of comments, snippets, timestamps, and private flags. The search index is asynchronous, so a newly added bookmark may not appear immediately. Use Hatena's manual export when you need an authoritative full backup rather than an indexed export.

For a manual backup or migration, [Hatena also officially supports](https://b.hatena.ne.jp/help/entry/port) exporting bookmark HTML, Atom, and RSS 1.0. Pass a downloaded export with `--input`:

```powershell
python -m feedian --config config.json --source hatena --input "C:\Users\you\Downloads\hatena-bookmarks.atom" --dry-run
python -m feedian --config config.json --source hatena --input "C:\Users\you\Downloads\hatena-bookmarks.atom"
```

The `--input` adapter also accepts an HTTP(S) RSS/Atom URL. Private bookmarks are included only when the selected file or URL contains them.

## Config

See `config.example.json`.

Important fields:

- `vault_path`: Absolute path to your Obsidian vault.
- `output_folder`: Folder inside the vault where notes are written.
- `hatena_input`: Optional default path or URL for a Hatena Atom, RSS 1.0, or bookmark HTML export.
- `hatena_output_folder`: Folder inside the vault for Hatena notes (default: `Hatena`).
- `hatena_base_tags`: Tags added to every Hatena note.
- `hatena_request_interval_seconds`: Minimum delay between authenticated Hatena export requests (default: `0.3`).
- `hatena_fetch_public_comments`: Fetch public Hatena comments for URLs from every source and write a linked comments note (default: `true`).
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
python -m feedian --config config.json --estimate
```

The default sample size is `10%`, with a minimum of 20 pages when the target has at least 20 bookmarks. Samples are proportional to Raindrop collections and evenly spaced within each collection's API order.

```powershell
# Use exactly 50 sampled pages.
python -m feedian --config config.json --estimate --estimate-sample-size 50

# Use 20% of the target bookmarks (at least 20 pages).
python -m feedian --config config.json --estimate --estimate-sample-size 20%

# Do not fetch pages; use the generic 2,000-10,000 input-token range instead.
python -m feedian --config config.json --estimate --estimate-sample-size 0

# Estimate metadata and excerpts only, without fetching linked pages.
python -m feedian --config config.json --estimate --skip-page-fetch
```

The command reports its current phase while it runs: official price refresh, bookmark collection, every 50 collected bookmarks, sample selection, each page fetch, and cost calculation. On each run it reads the [official OpenAI model catalog](https://developers.openai.com/api/docs/models) and prices for its recommended models, then also includes the configured `openai_model` (or `OPENAI_MODEL` override) and labels it `selected`. The `gpt-5.6` alias maps to Sol. If the catalog cannot be read or parsed, it prints `price_source=fallback` and uses the built-in price table instead. When pages are sampled, the table shows both a typical and a maximum estimate. The typical estimate uses the aggregate `output_tokens / input_tokens` ratio from matching usage records when available; otherwise it uses the initial `input-matched` assumption that output tokens equal the measured mean input tokens. The maximum estimate uses `max_output_tokens`. A failed page fetch still estimates the fallback prompt made from Raindrop metadata and the fetch error. Server-side request framing, reasoning tokens, or future price changes can still make the final bill differ.

## Behavior

- Normal note generation is read-only against Raindrop.io. `--sync-raindrop-summary` and `--sync-raindrop-tags` are explicit opt-in operations that write Raindrop notes or tags.
- Existing LLM-generated notes are skipped unless `--force` is passed. An LLM run automatically upgrades notes that were previously created with `--no-llm`; a later `--no-llm` run never downgrades an LLM-generated note.
- New LLM-generated filenames use the Japanese note title and include the Raindrop item ID to avoid collisions. Use `--rename-existing` to rename existing LLM notes from their stored note titles and to rename `--no-llm` notes when they are upgraded.
- Notes preserve the original Raindrop title and excerpt, and store the cleaned linked-page text as `Extracted Content (Original)`. Existing notes are not backfilled automatically.
- If a web page cannot be fetched, the tool still uses Raindrop metadata such as title and excerpt.
- HTML decoding uses w3lib and charset-normalizer. Main-content extraction uses Trafilatura in precision-oriented mode, with a Playwright headless-browser fallback for empty, corrupted, or JavaScript-shell static HTML. A short static article whose title and body were extracted is not replaced merely because the rendered page is longer; this avoids preferring ad-heavy browser output.
- Page fetching accepts only `http` and `https` URLs and rejects local/private network addresses by default, including after redirects. Set `allow_private_urls` only for a trusted internal bookmark collection.
- Linked-page text and bookmark metadata are treated as untrusted reference data when sent to the LLM; instructions in them are not followed.
- Raindrop and OpenAI requests retry transient 408, 409, 425, 429, 5xx, and network failures with bounded exponential backoff.
- Raindrop note and tag syncs pace each HTTP request independently at `sync_request_interval_seconds`. On a 429 response, the retry waits for the later of `Retry-After` and Raindrop's `X-RateLimit-Reset` time before retrying. Each sync prints its request interval at start and `elapsed=<seconds>s` when complete.
- Long-running commands show their phase and progress automatically. `--progress auto` (the default) uses Rich progress bars in an interactive terminal and periodic plain-text updates elsewhere. Use `--progress rich`, `--progress plain`, or `--progress off` to override this choice; add `--verbose` to include individual bookmark names.
- Before each OpenAI summary request, Feedian reads the usage log to confirm the vault is available. If the vault cannot be read or a Markdown or usage write fails, it stops before making further OpenAI requests.
- After an LLM response, Feedian temporarily stores at most one pending note under `~/.feedian/pending` until both the Markdown note and usage record are stored in the vault. The next LLM run automatically completes this pending write without requesting another summary; the local pending file is then removed.
- Successful LLM summaries append a JSON line with `operation: "summarize"` and a transaction ID to `<vault_path>/<output_folder>/.feedian-usage.jsonl`. Each line contains token usage, model and reasoning settings, a price snapshot, and the request's estimated USD cost; it does not contain page text or URLs.

## Useful Commands

List collections:

```powershell
python -m feedian --config config.json --list-collections
```

Process one collection:

```powershell
python -m feedian --config config.json --collection 123456 --limit 20
```

Upgrade `--no-llm` notes with LLM summaries and rename notes to their Japanese titles. Existing LLM notes are renamed from their saved frontmatter title without another OpenAI call:

```powershell
python -m feedian --config config.json --rename-existing
```

Preview copying existing Japanese LLM summaries into Raindrop notes. This reads local Markdown only and does not call OpenAI or modify Raindrop:

```powershell
python -m feedian --config config.json --sync-raindrop-summary --dry-run
```

Apply the summary sync. Feedian appends or updates only its managed `Feedian Summary` block in each Raindrop note, preserving any manual note text:

```powershell
python -m feedian --config config.json --sync-raindrop-summary
```

Preview tags from existing LLM notes before adding them to the matching Raindrop items:

```powershell
python -m feedian --config config.json --sync-raindrop-tags --dry-run
```

Apply the tag sync. Feedian excludes `base_tags`, reads the item's current Raindrop tags, and appends only missing tags. It never replaces or removes existing Raindrop tags:

```powershell
python -m feedian --config config.json --sync-raindrop-tags
```

Use a different vault without editing config:

```powershell
python -m feedian --config config.json --vault "C:\Users\you\Documents\Obsidian\Vault"
```

## License

This project is licensed under the [MIT License](LICENSE).
