from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Config, load_config
from .estimate import (
    ModelPrice,
    comparison_model,
    count_prompt_tokens,
    format_cost_rows,
    parse_sample_size,
    projected_costs,
    refresh_model_prices,
    select_sample,
    usage_cost_usd,
)
from .extract import PageFetchResult, fetch_page_text
from .llm import SUMMARY_INSTRUCTIONS, USAGE_FIELD, build_prompt, summarize_bookmark
from .markdown import normalize_tag, note_filename, render_note, upsert_raindrop_summary
from .raindrop import RaindropClient


NON_CONTENT_TAGS = frozenset({"x", "sns"})


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
        "--rename-existing",
        action="store_true",
        help="Rename existing LLM notes to their stored note titles and rename upgraded notes after summarizing.",
    )
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
        help="Estimate model API cost without calling a model or writing notes.",
    )
    parser.add_argument(
        "--estimate-sample-size",
        default="10%",
        metavar="SIZE",
        help="Sample size for --estimate: an integer, percentage, or 0 (default: 10%%).",
    )
    parser.add_argument(
        "--sync-raindrop-summary",
        action="store_true",
        help="Copy Japanese LLM summaries from existing notes into managed Raindrop note blocks.",
    )
    parser.add_argument(
        "--sync-raindrop-tags",
        action="store_true",
        help="Append LLM tags from existing notes to the matching Raindrop items.",
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


def has_llm_summary(note_path: Path) -> bool:
    try:
        text = note_path.read_text(encoding="utf-8")
    except OSError:
        return True
    if not text.startswith("---\n"):
        return True
    frontmatter, separator, _ = text[4:].partition("\n---\n")
    if not separator:
        return True
    for line in frontmatter.splitlines():
        if line.startswith("summary_model:"):
            value = line.partition(":")[2].strip().strip('"').strip("'")
            return bool(value)
    return False


def should_upgrade_note(note_path: Path, args: argparse.Namespace) -> bool:
    return not args.no_llm and not args.dry_run and not has_llm_summary(note_path)


def rename_existing_note(note_path: Path, destination: Path, item: dict[str, Any]) -> Path:
    try:
        text = note_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"could not read existing note for rename: {note_path}") from exc
    title = _frontmatter_values(text).get("title", "")
    target = destination / note_filename(item, title=title)
    if target == note_path:
        return note_path
    if target.exists():
        raise FileExistsError(f"rename target already exists: {target}")
    note_path.replace(target)
    return target


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


def build_usage_record(
    item: dict[str, Any],
    model: str,
    reasoning_effort: str,
    max_output_tokens: int,
    usage: dict[str, Any],
    price: ModelPrice | None,
    price_source: str,
    transaction_id: str | None = None,
) -> dict[str, Any]:
    return {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "transaction_id": transaction_id or str(uuid.uuid4()),
        "operation": "summarize",
        "raindrop_id": item.get("_id"),
        "model": model,
        "reasoning_effort": reasoning_effort,
        "max_output_tokens": max_output_tokens,
        "summary_format": "bookmark_note/v1",
        "input_tokens": usage.get("input_tokens", 0),
        "cached_input_tokens": usage.get("cached_input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "reasoning_tokens": usage.get("reasoning_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
        "price_source": price_source,
        "input_per_million_usd": price.input_per_million if price else None,
        "cached_input_per_million_usd": price.cached_input_per_million if price else None,
        "output_per_million_usd": price.output_per_million if price else None,
        "estimated_cost_usd": usage_cost_usd(usage, price) if price else None,
    }


def append_usage_record(destination: Path, record: dict[str, Any]) -> None:
    with (destination / ".raindian-usage.jsonl").open("a", encoding="utf-8", newline="\n") as usage_file:
        usage_file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def usage_record_exists(destination: Path, transaction_id: str) -> bool:
    usage_path = destination / ".raindian-usage.jsonl"
    if not usage_path.exists():
        return False
    with usage_path.open(encoding="utf-8") as usage_file:
        for line in usage_file:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and record.get("transaction_id") == transaction_id:
                return True
    return False


def usage_output_ratio(
    destination: Path,
    model: str,
    reasoning_effort: str,
) -> tuple[float, int] | None:
    usage_path = destination / ".raindian-usage.jsonl"
    if not usage_path.exists():
        return None
    input_total = 0
    output_total = 0
    record_count = 0
    for line in usage_path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if record.get("model") != model or record.get("reasoning_effort") != reasoning_effort:
            continue
        if record.get("operation") not in {None, "summarize"}:
            continue
        input_tokens = _non_negative_int(record.get("input_tokens"))
        output_tokens = _non_negative_int(record.get("output_tokens"))
        if input_tokens is None or output_tokens is None or input_tokens == 0:
            continue
        input_total += input_tokens
        output_total += output_tokens
        record_count += 1
    if input_total == 0:
        return None
    return output_total / input_total, record_count


def _non_negative_int(value: Any) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None


def process_bookmarks(config: Config, args: argparse.Namespace) -> int:
    started_at = time.perf_counter()
    raindrop_token = require_env("RAINDROP_TOKEN")
    openai_key = "" if args.no_llm or args.dry_run else require_env("OPENAI_API_KEY")
    model = os.environ.get("OPENAI_MODEL", config.openai_model)
    usage_price: ModelPrice | None = None
    usage_price_source = "unavailable"
    if not args.no_llm and not args.dry_run:
        print("usage: phase=refreshing-model-prices")
        price_refresh = refresh_model_prices(
            model,
            timeout_seconds=min(config.request_timeout_seconds, 10),
            include_recommended=False,
        )
        pricing_model = comparison_model(model)
        usage_price = next((price for price in price_refresh.prices if price.model == pricing_model), None)
        usage_price_source = (
            "fallback" if pricing_model in price_refresh.fallback_models else price_refresh.source
        )
        print(f"usage: price_source={usage_price_source}")
        if price_refresh.warning:
            print(f"usage: price_refresh_warning={price_refresh.warning}")
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
    renamed = 0
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
        existing = existing_note_for_item(destination, item)
        target = existing or (destination / note_filename(item))
        existing_path = existing or (target if target.exists() else None)
        if existing_path and not args.force:
            if args.rename_existing and has_llm_summary(existing_path):
                try:
                    renamed_path = rename_existing_note(existing_path, destination, item)
                except Exception as exc:
                    print(f"  rename warning: {exc}")
                    failed += 1
                else:
                    if renamed_path == existing_path:
                        print(f"skip existing: {existing_path}")
                    else:
                        print(f"rename: {existing_path.name} -> {renamed_path.name}")
                        renamed += 1
                    skipped += 1
                continue
            if should_upgrade_note(existing_path, args):
                print(f"upgrade no-llm note: {existing_path}")
                target = existing_path
            else:
                print(f"skip existing: {existing_path}")
                skipped += 1
                continue

        print(f"process: {item.get('title') or item.get('link')}")
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
        usage = summary.pop(USAGE_FIELD, None)

        rename_source: Path | None = None
        if existing is None:
            target = destination / note_filename(item, title=str(summary.get("note_title") or ""))
        elif args.rename_existing:
            renamed_target = destination / note_filename(item, title=str(summary.get("note_title") or ""))
            if renamed_target != existing:
                if renamed_target.exists():
                    print(f"  rename warning: rename target already exists: {renamed_target}")
                    failed += 1
                    continue
                target = renamed_target
                rename_source = existing
        print(f"  note: {target}")

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
            if rename_source is not None:
                try:
                    rename_source.unlink()
                    renamed += 1
                except OSError as exc:
                    print(f"  rename warning: could not remove old note: {exc}")
                    failed += 1
            created += 1
            if isinstance(usage, dict):
                try:
                    usage_record = build_usage_record(
                        item=item,
                        model=model,
                        reasoning_effort=config.openai_reasoning_effort,
                        max_output_tokens=config.max_output_tokens,
                        usage=usage,
                        price=usage_price,
                        price_source=usage_price_source,
                    )
                    append_usage_record(
                        destination=destination,
                        record=usage_record,
                    )
                except OSError as exc:
                    print(f"  usage log warning: {exc}")
                    failed += 1

        if config.sleep_seconds > 0:
            time.sleep(config.sleep_seconds)

    elapsed_seconds = time.perf_counter() - started_at
    print(
        f"done: processed={processed} created={created} renamed={renamed} skipped={skipped} "
        f"failed={failed} elapsed={elapsed_seconds:.1f}s"
    )
    return 1 if failed else 0


def sync_raindrop_summaries(config: Config, args: argparse.Namespace, client: RaindropClient | None) -> int:
    started_at = time.perf_counter()
    destination = output_dir(config)
    if not destination.exists():
        print(f"sync: no output folder: {destination}")
        return 0

    planned = 0
    updated = 0
    skipped = 0
    failed = 0
    notes = sorted(destination.glob("* - *.md"))
    for note_path in notes:
        candidate = summary_sync_candidate(note_path)
        if candidate is None:
            skipped += 1
            continue
        raindrop_id, summary = candidate
        if args.limit is not None and planned >= args.limit:
            break
        planned += 1
        print(f"sync: {note_path.name} -> raindrop_id={raindrop_id}")
        if args.dry_run:
            print("  dry-run: would update Raindrop note")
            continue
        if client is None:
            raise RuntimeError("Raindrop client is required when applying summary sync.")
        try:
            item = client.get_raindrop(raindrop_id)
            original_note = item.get("note")
            original_note = original_note if isinstance(original_note, str) else ""
            updated_note = upsert_raindrop_summary(original_note, summary)
            if updated_note == original_note:
                print("  sync: unchanged")
                continue
            if len(updated_note) > 10_000:
                raise ValueError("updated Raindrop note exceeds the 10,000-character limit")
            client.update_raindrop_note(raindrop_id, updated_note)
            updated += 1
        except Exception as exc:
            print(f"  sync warning: {exc}")
            failed += 1
        if config.sleep_seconds > 0:
            time.sleep(config.sleep_seconds)

    elapsed_seconds = time.perf_counter() - started_at
    print(
        f"done: sync_planned={planned} sync_updated={updated} skipped={skipped} "
        f"failed={failed} elapsed={elapsed_seconds:.1f}s"
    )
    return 1 if failed else 0


def summary_sync_candidate(note_path: Path) -> tuple[int, str] | None:
    try:
        text = note_path.read_text(encoding="utf-8")
    except OSError:
        return None
    frontmatter = _frontmatter_values(text)
    if not frontmatter.get("summary_model"):
        return None
    try:
        raindrop_id = int(frontmatter.get("raindrop_id", ""))
    except ValueError:
        return None
    summary_match = re.search(r"^## Summary\s*\n+(.*?)(?=^##\s|\Z)", text, flags=re.MULTILINE | re.DOTALL)
    if not summary_match:
        return None
    summary = summary_match.group(1).strip()
    return (raindrop_id, summary) if summary else None


def sync_raindrop_tags(config: Config, args: argparse.Namespace, client: RaindropClient | None) -> int:
    started_at = time.perf_counter()
    destination = output_dir(config)
    if not destination.exists():
        print(f"sync-tags: no output folder: {destination}")
        return 0

    planned = 0
    updated = 0
    skipped = 0
    failed = 0
    for note_path in sorted(destination.glob("* - *.md")):
        candidate = tag_sync_candidate(note_path, config.base_tags or [])
        if candidate is None:
            skipped += 1
            continue
        raindrop_id, collection_id, note_tags = candidate
        if args.limit is not None and planned >= args.limit:
            break
        planned += 1
        if args.dry_run:
            print(f"sync-tags: {note_path.name} -> raindrop_id={raindrop_id} tags={note_tags}")
            print("  dry-run: would append Raindrop tags")
            continue
        if client is None:
            raise RuntimeError("Raindrop client is required when applying tag sync.")
        try:
            item = client.get_raindrop(raindrop_id)
            existing_tags = {
                normalized
                for raw_tag in item.get("tags", [])
                if isinstance(raw_tag, str) and (normalized := normalize_tag(raw_tag))
            }
            missing_tags = [tag for tag in note_tags if tag not in existing_tags]
            if not missing_tags:
                print(f"sync-tags: {note_path.name} -> unchanged")
                continue
            print(f"sync-tags: {note_path.name} -> raindrop_id={raindrop_id} tags={missing_tags}")
            client.append_raindrop_tags(collection_id, raindrop_id, missing_tags)
            updated += 1
        except Exception as exc:
            print(f"  sync-tags warning: {exc}")
            failed += 1
        if config.sleep_seconds > 0:
            time.sleep(config.sleep_seconds)

    elapsed_seconds = time.perf_counter() - started_at
    print(
        f"done: tag_sync_planned={planned} tag_sync_updated={updated} skipped={skipped} "
        f"failed={failed} elapsed={elapsed_seconds:.1f}s"
    )
    return 1 if failed else 0


def tag_sync_candidate(note_path: Path, base_tags: list[str]) -> tuple[int, int, list[str]] | None:
    try:
        text = note_path.read_text(encoding="utf-8")
    except OSError:
        return None
    frontmatter = _frontmatter_values(text)
    if not frontmatter.get("summary_model"):
        return None
    try:
        raindrop_id = int(frontmatter.get("raindrop_id", ""))
        collection_id = int(frontmatter.get("raindrop_collection_id", ""))
    except ValueError:
        return None
    if "llm_tags" in frontmatter:
        raw_tags = _frontmatter_list(text, "llm_tags")
    else:
        raw_tags = _frontmatter_list(text, "tags")
        excluded_tags = {normalize_tag(tag) for tag in base_tags}
        raw_tags = [tag for tag in raw_tags if normalize_tag(tag) not in excluded_tags]
    tags = [tag for tag in _normalized_unique_tags(raw_tags) if tag not in NON_CONTENT_TAGS]
    return (raindrop_id, collection_id, tags) if tags else None


def _frontmatter_values(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        return {}
    frontmatter, separator, _ = text[4:].partition("\n---\n")
    if not separator:
        return {}
    values: dict[str, str] = {}
    for line in frontmatter.splitlines():
        key, delimiter, value = line.partition(":")
        if not delimiter:
            continue
        key = key.strip()
        value = value.strip()
        if value.startswith('"') and value.endswith('"'):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = value.strip('"')
        values[key] = value
    return values


def _frontmatter_list(text: str, field: str) -> list[str]:
    if not text.startswith("---\n"):
        return []
    frontmatter, separator, _ = text[4:].partition("\n---\n")
    if not separator:
        return []
    lines = frontmatter.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != f"{field}:":
            continue
        values: list[str] = []
        for entry in lines[index + 1 :]:
            if not entry.startswith("  - "):
                break
            value = entry[4:].strip()
            if value.startswith('"') and value.endswith('"'):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    value = value.strip('"')
            if isinstance(value, str):
                values.append(value)
        return values
    return []


def _normalized_unique_tags(tags: list[str]) -> list[str]:
    seen: set[str] = set()
    normalized_tags: list[str] = []
    for raw_tag in tags:
        tag = normalize_tag(raw_tag)
        if tag and tag not in seen:
            seen.add(tag)
            normalized_tags.append(tag)
    return normalized_tags


def estimate_bookmarks(config: Config, args: argparse.Namespace) -> int:
    started_at = time.perf_counter()
    raindrop_token = require_env("RAINDROP_TOKEN")
    model = os.environ.get("OPENAI_MODEL", config.openai_model)
    _estimate_progress("phase=refreshing-model-prices")
    price_refresh = refresh_model_prices(model, timeout_seconds=min(config.request_timeout_seconds, 10))
    print(f"estimate: price_source={price_refresh.source} url=https://developers.openai.com/api/docs/models")
    if price_refresh.warning:
        print(f"estimate: price_refresh_warning={price_refresh.warning}")
    client = RaindropClient(
        token=raindrop_token,
        timeout_seconds=config.request_timeout_seconds,
        max_retries=config.max_retries,
        retry_base_seconds=config.retry_base_seconds,
    )
    _estimate_progress("phase=collecting-bookmarks")
    items: list[dict[str, Any]] = []
    for item in client.iter_raindrops(
        collection_id=config.collection_id,
        per_page=config.per_page,
        nested=config.nested,
        limit=args.limit,
    ):
        items.append(item)
        if len(items) % config.per_page == 0:
            _estimate_progress(f"collected={len(items)}")
    _estimate_progress(f"phase=sampling collected={len(items)}")
    population = len(items)
    if population == 0:
        print("estimate: target=0")
        return 0
    sample_size = parse_sample_size(args.estimate_sample_size, population)
    if sample_size == 0:
        _estimate_progress("phase=calculating-costs count-only=true")
        _print_count_only_estimate(population, config.max_output_tokens, model, price_refresh.prices)
        _print_elapsed(started_at)
        return 0

    samples = select_sample(items, sample_size)
    _estimate_progress(f"phase=fetching-pages sample={len(samples)}")
    token_counts: list[int] = []
    failures: Counter[str] = Counter()
    tokenizer_fallbacks: set[str] = set()
    for sample_index, item in enumerate(samples, start=1):
        _estimate_progress(
            f"fetching={sample_index}/{len(samples)} bookmark={_estimate_bookmark_label(item)}"
        )
        url = item.get("link")
        page = PageFetchResult(url=url if isinstance(url, str) else "", text="", title="", error=None)
        if not args.skip_page_fetch and page.url:
            page = fetch_page_text(
                page.url,
                timeout_seconds=config.request_timeout_seconds,
                max_chars=config.max_article_chars,
                allow_private_urls=config.allow_private_urls,
            )
            if page.error:
                failures[page.error] += 1
            if config.sleep_seconds > 0:
                time.sleep(config.sleep_seconds)
        prompt = build_prompt(item=item, page=page, language=config.language)
        token_count, fallback = count_prompt_tokens(f"{SUMMARY_INSTRUCTIONS}\n\n{prompt}", model)
        token_counts.append(token_count)
        if fallback:
            tokenizer_fallbacks.add(fallback)

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
    if args.skip_page_fetch:
        print("estimate: page_fetch=skipped")
    print(
        f"estimate: mean_input_tokens={mean_tokens:.0f} "
        f"projected_input_tokens={projected_input_tokens:.0f} "
        f"max_output_tokens={config.max_output_tokens}"
    )
    for encoding_name in sorted(tokenizer_fallbacks):
        print(f"estimate: tokenizer fallback={encoding_name}")
    for reason, count in failures.most_common():
        print(f"  page fetch failure: {count} x {reason}")
    _estimate_progress("phase=calculating-costs")
    historical_usage = usage_output_ratio(
        output_dir(config),
        model,
        config.openai_reasoning_effort,
    )
    if historical_usage:
        output_ratio, usage_records = historical_usage
        typical_output_tokens = min(mean_tokens * output_ratio, config.max_output_tokens)
        print(
            f"estimate: assumed_output_tokens={typical_output_tokens:.0f} "
            f"(usage-ratio records={usage_records})"
        )
    else:
        typical_output_tokens = min(mean_tokens, config.max_output_tokens)
        print(f"estimate: assumed_output_tokens={typical_output_tokens:.0f} (input-matched)")
    maximum_rows = projected_costs(
        population,
        mean_tokens,
        config.max_output_tokens,
        price_refresh.prices,
    )
    typical_rows = projected_costs(
        population,
        mean_tokens,
        typical_output_tokens,
        price_refresh.prices,
    )
    for line in format_cost_rows(
        maximum_rows,
        selected_model=model,
        typical_rows=typical_rows,
    ):
        print(line)
    _print_elapsed(started_at)
    return 0


def _print_count_only_estimate(
    population: int,
    max_output_tokens: int,
    selected_model: str,
    prices: tuple[ModelPrice, ...],
) -> None:
    lower = projected_costs(population, 2_000, max_output_tokens, prices)
    upper = projected_costs(population, 10_000, max_output_tokens, prices)
    print(
        f"estimate: target={population} sample=0 count-only=true "
        "input_tokens_per_item=2000-10000 (generic range)"
    )
    print(f"estimate: max_output_tokens={max_output_tokens}")
    selected_pricing_model = comparison_model(selected_model)
    if not any(row.model == selected_pricing_model for row in lower):
        print(f"selected model: {selected_model} (not in comparison table)")
    print("model\ttotal (max)")
    for lower_row, upper_row in zip(lower, upper):
        selected = " [selected]" if lower_row.model == selected_pricing_model else ""
        print(f"{lower_row.name}{selected}\t${lower_row.total_cost:.2f}-${upper_row.total_cost:.2f}")


def _print_elapsed(started_at: float) -> None:
    print(f"done: elapsed={time.perf_counter() - started_at:.1f}s")


def _estimate_progress(message: str) -> None:
    print(f"estimate: {message}", flush=True)


def _estimate_bookmark_label(item: dict[str, Any]) -> str:
    label = item.get("title") or item.get("link") or item.get("_id") or "(untitled)"
    return str(label).replace("\r", " ").replace("\n", " ")[:80]


def main(argv: list[str] | None = None) -> int:
    configure_console_output()
    args = parse_args(argv or sys.argv[1:])
    try:
        load_env_file(Path(".env"))
        config = apply_overrides(load_config(args.config), args)
        sync_actions = int(args.sync_raindrop_summary) + int(args.sync_raindrop_tags)
        if sync_actions > 1:
            raise ValueError("Choose either --sync-raindrop-summary or --sync-raindrop-tags.")
        if args.estimate and (args.dry_run or args.list_collections or sync_actions):
            raise ValueError("--estimate cannot be combined with --dry-run, --list-collections, or sync options.")
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
        if args.sync_raindrop_summary:
            if args.dry_run:
                return sync_raindrop_summaries(config, args, client=None)
            raindrop_token = require_env("RAINDROP_TOKEN")
            client = RaindropClient(
                token=raindrop_token,
                timeout_seconds=config.request_timeout_seconds,
                max_retries=config.max_retries,
                retry_base_seconds=config.retry_base_seconds,
            )
            return sync_raindrop_summaries(config, args, client)
        if args.sync_raindrop_tags:
            if args.dry_run:
                return sync_raindrop_tags(config, args, client=None)
            raindrop_token = require_env("RAINDROP_TOKEN")
            client = RaindropClient(
                token=raindrop_token,
                timeout_seconds=config.request_timeout_seconds,
                max_retries=config.max_retries,
                retry_base_seconds=config.retry_base_seconds,
            )
            return sync_raindrop_tags(config, args, client)
        return process_bookmarks(config, args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
