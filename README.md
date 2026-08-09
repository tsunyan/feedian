# rainian

Raindrop.io bookmarks to Obsidian Markdown notes.

`rainian` reads bookmarks from the Raindrop.io REST API, fetches each linked page, asks the OpenAI Responses API for a Japanese summary and tags, and writes one `.md` file per bookmark into a local Obsidian vault.

## Requirements

- Python 3.11+
- A Raindrop.io access token
- An OpenAI API key
- A local Obsidian vault folder

This tool uses only the Python standard library. No package install is required.

For personal use, Raindrop.io lets you copy a test token from your application settings in the App Management Console. Use that value as `RAINDROP_TOKEN`.

## Setup

Copy the example config and edit the vault path:

```powershell
Copy-Item config.example.json config.json
notepad config.json
```

Set API keys in your shell:

```powershell
$env:RAINDROP_TOKEN = "your-raindrop-token"
$env:OPENAI_API_KEY = "your-openai-api-key"
```

Or copy `.env.example` to `.env` and edit it. `.env` is ignored by Git.

Run a dry run first:

```powershell
python -m rainian --config config.json --limit 3 --dry-run
```

Create notes:

```powershell
python -m rainian --config config.json --limit 10
```

## Config

See `config.example.json`.

Important fields:

- `vault_path`: Absolute path to your Obsidian vault.
- `output_folder`: Folder inside the vault where notes are written.
- `collection_id`: Raindrop collection ID. Use `0` for all bookmarks.
- `nested`: Include nested collections when reading a collection.
- `base_tags`: Tags added to every generated note.
- `openai_model`: Model used for summarization. Override with `OPENAI_MODEL` if your account uses a different model.

## Behavior

- Raindrop.io is read-only. This tool does not edit bookmark tags in Raindrop.
- Existing notes are skipped unless `--force` is passed.
- Filenames include the Raindrop item ID to avoid collisions.
- If a web page cannot be fetched, the tool still uses Raindrop metadata such as title and excerpt.

## Useful Commands

List collections:

```powershell
python -m rainian --config config.json --list-collections
```

Process one collection:

```powershell
python -m rainian --config config.json --collection 123456 --limit 20
```

Use a different vault without editing config:

```powershell
python -m rainian --config config.json --vault "C:\Users\you\Documents\Obsidian\Vault"
```
