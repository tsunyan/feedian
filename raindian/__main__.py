from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Config, load_config
from .extract import PageFetchResult, fetch_page_text
from .llm import summarize_bookmark
from .markdown import note_filename, render_note
from .raindrop import RaindropClient


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="raindian",
        description="Export Raindrop.io bookmarks as summarized Obsidian Markdown notes.",
    )
    parser.add_argument("--config", default="config.json", help="Path to config JSON.")
    parser.add_argument("--vault", help="Override Obsidian vault path.")
    parser.add_argument("--folder", help="Override output folder inside the vault.")
    parser.add_argument("--collection", type=int, help="Override Raindrop collection ID.")
    parser.add_argument("--limit", type=int, help="Maximum bookmarks to process.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview Raindrop items without fetching pages, calling OpenAI, or writing files.",
    )
    parser.add_argument("--force", action="store_true", help="Overwrite existing notes.")
    parser.add_argument(
        "--list-collections",
        action="store_true",
        help="List Raindrop collections and exit.",
    )
    parser.add_argument(
        "--skip-page-fetch",
        action="store_true",
        help="Use only Raindrop metadata and excerpt.",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Create notes from metadata without calling OpenAI.",
    )
    return parser.parse_args(argv)


def apply_overrides(config: Config, args: argparse.Namespace) -> Config:
    values = config.model_copy()
    if args.vault:
        values.vault_path = args.vault
    if args.folder:
        values.output_folder = args.folder
    if args.collection is not None:
        values.collection_id = args.collection
    return values


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def configure_console_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")


def fallback_summary(item: dict[str, Any], page: PageFetchResult) -> dict[str, Any]:
    tags = item.get("tags") or []
    excerpt = (item.get("excerpt") or "").strip()
    text = page.text.strip() or excerpt
    first_paragraph = ""
    for part in text.split("\n"):
        part = part.strip()
        if part:
            first_paragraph = part[:500]
            break
    return {
        "note_title": item.get("title") or item.get("link") or "Untitled bookmark",
        "summary": excerpt or first_paragraph or "Summary unavailable.",
        "key_points": [],
        "tags": tags[:8],
        "content_type": item.get("type") or "link",
    }


def list_collections(client: RaindropClient) -> None:
    roots = client.get_root_collections()
    children = client.get_child_collections()
    all_items = roots + children
    if not all_items:
        print("No collections found.")
        return
    for item in sorted(all_items, key=lambda entry: (entry.get("parent", {}).get("$id", 0), entry.get("title", ""))):
        collection_id = item.get("_id")
        title = item.get("title", "(untitled)")
        count = item.get("count", 0)
        parent = item.get("parent", {}).get("$id")
        parent_text = f" parent={parent}" if parent else ""
        print(f"{collection_id}\t{title}\tcount={count}{parent_text}")


def output_dir(config: Config) -> Path:
    vault = Path(config.vault_path).expanduser()
    return vault / config.output_folder


def existing_note_for_item(destination: Path, item: dict[str, Any]) -> Path | None:
    item_id = item.get("_id")
    if item_id is None:
        return None
    matches = sorted(destination.glob(f"* - {item_id}.md"))
    return matches[0] if matches else None


def write_note_atomically(target: Path, markdown: str) -> None:
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(markdown, encoding="utf-8", newline="\n")
        temporary.replace(target)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def process_bookmarks(config: Config, args: argparse.Namespace) -> int:
    started_at = time.perf_counter()
    raindrop_token = require_env("RAINDROP_TOKEN")
    openai_key = "" if args.no_llm or args.dry_run else require_env("OPENAI_API_KEY")
    model = os.environ.get("OPENAI_MODEL", config.openai_model)
    client = RaindropClient(
        token=raindrop_token,
        timeout_seconds=config.request_timeout_seconds,
    )
    destination = output_dir(config)
    if not args.dry_run:
        destination.mkdir(parents=True, exist_ok=True)

    processed = 0
    created = 0
    skipped = 0
    failed = 0
    now = datetime.now(timezone.utc).isoformat()
    if args.dry_run:
        print("dry-run: page fetching and OpenAI calls are disabled")

    for item in client.iter_raindrops(
        collection_id=config.collection_id,
        per_page=config.per_page,
        nested=config.nested,
        limit=args.limit,
    ):
        processed += 1
        filename = note_filename(item)
        existing = existing_note_for_item(destination, item)
        target = existing or (destination / filename)
        if existing and not args.force:
            print(f"skip existing: {existing}")
            skipped += 1
            continue
        if target.exists() and not args.force:
            print(f"skip existing: {target}")
            skipped += 1
            continue

        print(f"process: {item.get('title') or item.get('link')} -> {target}")
        page = PageFetchResult(url=item.get("link", ""), text="", title="", error=None)
        if not args.dry_run and not args.skip_page_fetch and item.get("link"):
            page = fetch_page_text(
                item["link"],
                timeout_seconds=config.request_timeout_seconds,
                max_chars=config.max_article_chars,
            )
            if page.error:
                print(f"  page fetch warning: {page.error}")

        if args.no_llm or args.dry_run:
            summary = fallback_summary(item, page)
        else:
            try:
                summary = summarize_bookmark(
                    api_key=openai_key,
                    model=model,
                    item=item,
                    page=page,
                    language=config.language,
                    timeout_seconds=config.request_timeout_seconds,
                    max_output_tokens=config.max_output_tokens,
                    reasoning_effort=config.openai_reasoning_effort,
                )
            except Exception as exc:
                print(f"  summary warning: {exc}")
                failed += 1
                continue

        markdown = render_note(
            item=item,
            page=page,
            summary=summary,
            base_tags=config.base_tags,
            generated_at=now,
            model=None if args.no_llm else model,
        )
        if args.dry_run:
            print(f"  dry-run: would write {len(markdown)} characters")
        else:
            try:
                write_note_atomically(target, markdown)
            except Exception as exc:
                print(f"  write warning: {exc}")
                failed += 1
                continue
            created += 1

        if config.sleep_seconds > 0:
            time.sleep(config.sleep_seconds)

    elapsed_seconds = time.perf_counter() - started_at
    print(
        f"done: processed={processed} created={created} skipped={skipped} "
        f"failed={failed} elapsed={elapsed_seconds:.1f}s"
    )
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    configure_console_output()
    args = parse_args(argv or sys.argv[1:])
    try:
        load_env_file(Path(".env"))
        config = apply_overrides(load_config(args.config), args)
        if args.list_collections:
            raindrop_token = require_env("RAINDROP_TOKEN")
            client = RaindropClient(
                token=raindrop_token,
                timeout_seconds=config.request_timeout_seconds,
            )
            list_collections(client)
            return 0
        return process_bookmarks(config, args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
