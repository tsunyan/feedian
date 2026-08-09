from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Config, load_config
from .estimate import count_prompt_tokens, format_cost_rows, parse_sample_size, projected_costs, select_sample
from .extract import PageFetchResult, fetch_page_text
from .llm import SUMMARY_INSTRUCTIONS, build_prompt, summarize_bookmark
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
    parser.add_argument(
        "--estimate",
        action="store_true",
        help="Estimate OpenAI API cost without calling OpenAI or writing notes.",
    )
    parser.add_argument(
        "--estimate-sample-size",
        default="10%",
        metavar="SIZE",
        help="Sample size for --estimate: an integer, percentage, or 0 (default: 10%%).",
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
    all_items: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for item in roots + children:
        collection_id = item.get("_id")
        if isinstance(collection_id, int) and collection_id in seen_ids:
            continue
        if isinstance(collection_id, int):
            seen_ids.add(collection_id)
        all_items.append(item)
    if not all_items:
        print("No collections found.")
        return
    for item in sorted(all_items, key=lambda entry: (collection_parent_id(entry), entry.get("title", ""))):
        collection_id = item.get("_id")
        title = item.get("title", "(untitled)")
        count = item.get("count", 0)
        parent = collection_parent_id(item)
        parent_text = f" parent={parent}" if parent else ""
        print(f"{collection_id}\t{title}\tcount={count}{parent_text}")


def collection_parent_id(item: dict[str, Any]) -> int:
    parent = item.get("parent")
    if not isinstance(parent, dict):
        return 0
    parent_id = parent.get("$id")
    return parent_id if isinstance(parent_id, int) else 0


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
        max_retries=config.max_retries,
        retry_base_seconds=config.retry_base_seconds,
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
                allow_private_urls=config.allow_private_urls,
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
                    max_retries=config.max_retries,
                    retry_base_seconds=config.retry_base_seconds,
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


def estimate_bookmarks(config: Config, args: argparse.Namespace) -> int:
    started_at = time.perf_counter()
    raindrop_token = require_env("RAINDROP_TOKEN")
    model = os.environ.get("OPENAI_MODEL", config.openai_model)
    client = RaindropClient(
        token=raindrop_token,
        timeout_seconds=config.request_timeout_seconds,
        max_retries=config.max_retries,
        retry_base_seconds=config.retry_base_seconds,
    )
    items = list(
        client.iter_raindrops(
            collection_id=config.collection_id,
            per_page=config.per_page,
            nested=config.nested,
            limit=args.limit,
        )
    )
    population = len(items)
    if population == 0:
        print("estimate: target=0")
        return 0
    sample_size = parse_sample_size(args.estimate_sample_size, population)
    if sample_size == 0:
        _print_count_only_estimate(population, config.max_output_tokens, model)
        _print_elapsed(started_at)
        return 0

    samples = select_sample(items, sample_size)
    token_counts: list[int] = []
    failures: Counter[str] = Counter()
    tokenizer_fallbacks: set[str] = set()
    for item in samples:
        url = item.get("link")
        if not isinstance(url, str) or not url:
            failures["bookmark has no URL"] += 1
            continue
        page = fetch_page_text(
            url,
            timeout_seconds=config.request_timeout_seconds,
            max_chars=config.max_article_chars,
            allow_private_urls=config.allow_private_urls,
        )
        if page.error:
            failures[page.error] += 1
            continue
        prompt = build_prompt(item=item, page=page, language=config.language)
        token_count, fallback = count_prompt_tokens(f"{SUMMARY_INSTRUCTIONS}\n\n{prompt}", model)
        token_counts.append(token_count)
        if fallback:
            tokenizer_fallbacks.add(fallback)
        if config.sleep_seconds > 0:
            time.sleep(config.sleep_seconds)

    if not token_counts:
        print(f"estimate: target={population} sample={len(samples)} sampled=0 failed={sum(failures.values())}")
        for reason, count in failures.most_common():
            print(f"  page fetch failure: {count} x {reason}")
        _print_elapsed(started_at)
        return 1

    mean_tokens = sum(token_counts) / len(token_counts)
    projected_input_tokens = population * mean_tokens
    print(
        f"estimate: target={population} sample={len(samples)} sampled={len(token_counts)} "
        f"failed={sum(failures.values())}"
    )
    print(
        f"estimate: mean_input_tokens={mean_tokens:.0f} "
        f"projected_input_tokens={projected_input_tokens:.0f} "
        f"max_output_tokens={config.max_output_tokens}"
    )
    for encoding_name in sorted(tokenizer_fallbacks):
        print(f"estimate: tokenizer fallback={encoding_name}")
    for reason, count in failures.most_common():
        print(f"  page fetch failure: {count} x {reason}")
    for line in format_cost_rows(
        projected_costs(population, mean_tokens, config.max_output_tokens),
        selected_model=model,
    ):
        print(line)
    _print_elapsed(started_at)
    return 0


def _print_count_only_estimate(population: int, max_output_tokens: int, selected_model: str) -> None:
    lower = projected_costs(population, 2_000, max_output_tokens)
    upper = projected_costs(population, 10_000, max_output_tokens)
    print(
        f"estimate: target={population} sample=0 count-only=true "
        "input_tokens_per_item=2000-10000 (generic range)"
    )
    print(f"estimate: max_output_tokens={max_output_tokens}")
    print("model\ttotal (max)")
    for lower_row, upper_row in zip(lower, upper):
        selected = " [selected]" if lower_row.model == selected_model else ""
        print(f"{lower_row.name}{selected}\t${lower_row.total_cost:.2f}-${upper_row.total_cost:.2f}")


def _print_elapsed(started_at: float) -> None:
    print(f"done: elapsed={time.perf_counter() - started_at:.1f}s")


def main(argv: list[str] | None = None) -> int:
    configure_console_output()
    args = parse_args(argv or sys.argv[1:])
    try:
        load_env_file(Path(".env"))
        config = apply_overrides(load_config(args.config), args)
        if args.estimate and (args.dry_run or args.list_collections):
            raise ValueError("--estimate cannot be combined with --dry-run or --list-collections.")
        if args.list_collections:
            raindrop_token = require_env("RAINDROP_TOKEN")
            client = RaindropClient(
                token=raindrop_token,
                timeout_seconds=config.request_timeout_seconds,
                max_retries=config.max_retries,
                retry_base_seconds=config.retry_base_seconds,
            )
            list_collections(client)
            return 0
        if args.estimate:
            return estimate_bookmarks(config, args)
        return process_bookmarks(config, args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
