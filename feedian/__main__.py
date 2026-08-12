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
from typing import Any, Callable, Iterable, Iterator, TypeVar

from .canonical import canonical_item_from_metadata
from .cli import is_modern_command, main as modern_main
from .cli_ui import print_cli_error
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
from .env import load_env_file
from .extract import PageFetchResult, fetch_page_text
from .llm import SUMMARY_INSTRUCTIONS, USAGE_FIELD, build_prompt, summarize_bookmark
from .hatena import (
    HatenaEntryDiscussion,
    fetch_hatena_bookmarks,
    fetch_hatena_entry_discussion,
    load_hatena_export,
)
from .markdown import (
    canonical_note_filename,
    comments_note_filename,
    normalize_tag,
    note_filename,
    render_canonical_note,
    render_comments_note,
    render_note,
    upsert_raindrop_summary,
)
from .progress import PROGRESS_MODES, ProgressReporter
from .raindrop import RaindropClient
from .recovery import PendingTransaction, load_pending, remove_pending, save_pending
from .source_state import load_raindrop_collection_count, save_raindrop_collection_count


NON_CONTENT_TAGS = frozenset({"x", "sns"})
PENDING_STATE_ROOT = Path.home() / ".feedian" / "pending"
T = TypeVar("T")


def fetch_url_comments(
    config: Config,
    url: str,
    reporter: ProgressReporter | None = None,
) -> HatenaEntryDiscussion:
    if not config.hatena_fetch_public_comments or not url:
        return HatenaEntryDiscussion()
    try:
        return fetch_hatena_entry_discussion(
            url,
            timeout_seconds=config.request_timeout_seconds,
            max_retries=config.max_retries,
            retry_base_seconds=config.retry_base_seconds,
        )
    except Exception as exc:
        report(reporter, f"  Hatena comments warning: {exc}", verbose=True)
        return HatenaEntryDiscussion()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="feedian",
        description="Export bookmarks and feeds as summarized Obsidian Markdown notes.",
    )
    parser.add_argument(
        "--source",
        choices=("raindrop", "hatena"),
        default="raindrop",
        help="Source adapter to run (default: raindrop).",
    )
    parser.add_argument("--input", dest="source_input", help="Input file or URL for file/feed sources.")
    parser.add_argument("--config", default="config.json", help="Path to config JSON.")
    parser.add_argument("--vault", help="Override Obsidian vault path.")
    parser.add_argument("--folder", help="Override output folder inside the vault.")
    parser.add_argument("--collection", type=int, help="Override Raindrop collection ID.")
    parser.add_argument("--limit", type=int, help="Maximum bookmarks to process.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview source items without fetching pages, calling OpenAI, or writing files.",
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
        help="Use only source metadata, comments, and excerpts.",
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
    parser.add_argument(
        "--progress",
        choices=PROGRESS_MODES,
        default="auto",
        help="Progress display mode: auto, rich, plain, or off (default: auto).",
    )
    parser.add_argument("--verbose", action="store_true", help="Show per-bookmark processing details.")
    return parser.parse_args(argv)


def apply_overrides(config: Config, args: argparse.Namespace) -> Config:
    values = config.model_copy()
    if args.vault:
        values.vault_path = args.vault
    if args.folder:
        if args.source == "hatena":
            values.hatena_output_folder = args.folder
        else:
            values.output_folder = args.folder
    if args.collection is not None:
        values.collection_id = args.collection
    return values


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def configure_console_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")


def fallback_summary(item: dict[str, Any], page: PageFetchResult) -> dict[str, Any]:
    tags = item.get("tags") or []
    excerpt = (item.get("excerpt") or "").strip()
    note = (item.get("note") or "").strip()
    text = page.text.strip() or excerpt
    first_paragraph = ""
    for part in text.split("\n"):
        part = part.strip()
        if part:
            first_paragraph = part[:500]
            break
    return {
        "note_title": item.get("title") or item.get("link") or "Untitled bookmark",
        "summary": excerpt or note or first_paragraph or "Summary unavailable.",
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


def source_output_dir(config: Config, source: str) -> Path:
    vault = Path(config.vault_path).expanduser()
    return vault / (config.hatena_output_folder if source == "hatena" else config.output_folder)


def existing_note_for_item(destination: Path, item: dict[str, Any]) -> Path | None:
    item_id = item.get("_id")
    if item_id is None:
        return None
    matches = sorted(destination.glob(f"* - {item_id}.md"))
    return preferred_note_path(matches) if matches else None


def preferred_note_path(note_paths: list[Path]) -> Path:
    return max(note_paths, key=note_preference_key)


def note_preference_key(note_path: Path) -> tuple[bool, float, float, str]:
    try:
        text = note_path.read_text(encoding="utf-8")
    except OSError:
        return (True, 0.0, 0.0, note_path.name)

    frontmatter = _frontmatter_values(text)
    has_summary = bool(frontmatter.get("summary_model"))
    generated_at = frontmatter.get("summary_generated_at", "")
    try:
        summary_timestamp = datetime.fromisoformat(generated_at.replace("Z", "+00:00")).timestamp()
    except ValueError:
        summary_timestamp = 0.0
    try:
        modified_timestamp = note_path.stat().st_mtime
    except OSError:
        modified_timestamp = 0.0
    return (has_summary, summary_timestamp, modified_timestamp, note_path.name)


def unique_note_candidates(
    note_paths: list[Path],
    candidate_for_path: Callable[[Path], tuple[Any, ...] | None],
    on_path: Callable[[], None] | None = None,
) -> tuple[list[tuple[Path, tuple[Any, ...]]], int]:
    candidates: dict[int, tuple[Path, tuple[Any, ...]]] = {}
    skipped = 0
    for note_path in note_paths:
        try:
            candidate = candidate_for_path(note_path)
            if candidate is None:
                skipped += 1
                continue
            raindrop_id = candidate[0]
            if not isinstance(raindrop_id, int):
                skipped += 1
                continue
            current = candidates.get(raindrop_id)
            if current is None or note_preference_key(note_path) > note_preference_key(current[0]):
                candidates[raindrop_id] = (note_path, candidate)
            if current is not None:
                skipped += 1
        finally:
            if on_path is not None:
                on_path()
    return sorted(candidates.values(), key=lambda entry: entry[0].name), skipped


def tracked_items(items: Iterable[T], reporter: ProgressReporter | None) -> Iterator[T]:
    for item in items:
        try:
            yield item
        finally:
            if reporter is not None:
                reporter.advance()


def report(reporter: ProgressReporter | None, message: str, *, verbose: bool = False) -> None:
    if reporter is None:
        print(message)
    elif verbose:
        if reporter.verbose:
            reporter.log(message)
    elif reporter.mode == "off":
        print(message)
    else:
        reporter.log(message)


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
    old_comments = destination / comments_note_filename(note_path.name)
    if not old_comments.exists():
        note_path.replace(target)
        return target

    target_comments = destination / comments_note_filename(target.name)
    if target_comments.exists():
        raise FileExistsError(f"comments rename target already exists: {target_comments}")
    try:
        comments_text = old_comments.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"could not read comments note for rename: {old_comments}") from exc
    updated_note = text.replace(f"[[{old_comments.stem}]]", f"[[{target_comments.stem}]]")
    updated_comments = comments_text.replace(f"[[{note_path.stem}]]", f"[[{target.stem}]]")
    write_note_atomically(target_comments, updated_comments)
    write_note_atomically(target, updated_note)
    old_comments.unlink()
    note_path.unlink()
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
    source = str(item.get("_feedian_source") or "raindrop")
    source_id = item.get("_feedian_source_id") or item.get("_id")
    record = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "transaction_id": transaction_id or str(uuid.uuid4()),
        "operation": "summarize",
        "source": source,
        "source_id": source_id,
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
    if source == "raindrop":
        record["raindrop_id"] = item.get("_id")
    return record


def append_usage_record(destination: Path, record: dict[str, Any]) -> None:
    with (destination / ".feedian-usage.jsonl").open("a", encoding="utf-8", newline="\n") as usage_file:
        usage_file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def usage_record_exists(destination: Path, transaction_id: str) -> bool:
    usage_path = destination / ".feedian-usage.jsonl"
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


def ensure_usage_log_readable(destination: Path) -> None:
    usage_path = destination / ".feedian-usage.jsonl"
    with usage_path.open("a+", encoding="utf-8", newline="\n") as usage_file:
        usage_file.seek(0)
        usage_file.read(1)


def recover_pending_transaction(destination: Path) -> bool:
    transaction = load_pending(destination, state_root=PENDING_STATE_ROOT)
    if transaction is None:
        return False

    print(f"recovery: {transaction.target}")
    ensure_usage_log_readable(destination)
    if not transaction.target.exists():
        write_note_atomically(transaction.target, transaction.markdown)
    if not usage_record_exists(destination, transaction.transaction_id):
        append_usage_record(destination, transaction.usage_record)
    remove_pending(destination, state_root=PENDING_STATE_ROOT)
    print("  recovery: completed")
    return True


def usage_output_ratio(
    destination: Path,
    model: str,
    reasoning_effort: str,
) -> tuple[float, int] | None:
    usage_path = destination / ".feedian-usage.jsonl"
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


def process_bookmarks(
    config: Config,
    args: argparse.Namespace,
    reporter: ProgressReporter | None = None,
) -> int:
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
        try:
            destination.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"vault unavailable: {exc}")
            return 1
    if not args.no_llm and not args.dry_run:
        try:
            recover_pending_transaction(destination)
        except (OSError, ValueError) as exc:
            print(f"vault recovery warning: {exc}")
            return 1

    processed = 0
    created = 0
    renamed = 0
    skipped = 0
    failed = 0
    now = datetime.now(timezone.utc).isoformat()
    if args.dry_run:
        print("dry-run: page fetching and OpenAI calls are disabled")

    items: Iterable[dict[str, Any]] = client.iter_raindrops(
        collection_id=config.collection_id,
        per_page=config.per_page,
        nested=config.nested,
        limit=args.limit,
    )
    if reporter is not None:
        previous_count = None
        if args.limit is None:
            previous_count = load_raindrop_collection_count(
                raindrop_token,
                config.collection_id,
                config.nested,
            )
        collection_total = args.limit if args.limit is not None else previous_count
        reporter.start_task(
            "process: collecting Raindrop bookmarks",
            total=collection_total,
            estimated_total=previous_count is not None,
        )
        items = list(tracked_items(items, reporter))
        collected = len(items)
        if args.limit is None:
            report(reporter, f"process: collected={collected}{_format_count_delta(collected, previous_count)}")
            try:
                save_raindrop_collection_count(
                    raindrop_token,
                    config.collection_id,
                    config.nested,
                    collected,
                )
            except OSError as exc:
                report(reporter, f"process: count cache warning: {exc}")
        reporter.start_task("process: generating Obsidian notes", total=len(items))

    for item in items:
        processed += 1
        # Count every bookmark as soon as its processing starts, including
        # bookmarks that will be skipped because their note already exists.
        if reporter is not None:
            reporter.advance()
        existing = existing_note_for_item(destination, item)
        target = existing or (destination / note_filename(item))
        existing_path = existing or (target if target.exists() else None)
        if existing_path and not args.force:
            if args.rename_existing and has_llm_summary(existing_path):
                try:
                    renamed_path = rename_existing_note(existing_path, destination, item)
                except Exception as exc:
                    report(reporter, f"  rename warning: {exc}")
                    failed += 1
                else:
                    if renamed_path == existing_path:
                        report(reporter, f"skip existing: {existing_path}", verbose=True)
                    else:
                        report(reporter, f"rename: {existing_path.name} -> {renamed_path.name}", verbose=True)
                        renamed += 1
                    skipped += 1
                continue
            if should_upgrade_note(existing_path, args):
                report(reporter, f"upgrade no-llm note: {existing_path}", verbose=True)
                target = existing_path
            else:
                report(reporter, f"skip existing: {existing_path}", verbose=True)
                skipped += 1
                continue

        report(reporter, f"process: {item.get('title') or item.get('link')}", verbose=True)
        page = PageFetchResult(url=item.get("link", ""), text="", title="", error=None)
        if not args.dry_run and not args.skip_page_fetch and item.get("link"):
            page = fetch_page_text(
                item["link"],
                timeout_seconds=config.request_timeout_seconds,
                max_chars=config.max_article_chars,
                allow_private_urls=config.allow_private_urls,
            )
            if page.error:
                report(reporter, f"  page fetch warning: {page.error}", verbose=True)

        public_comments = (
            fetch_url_comments(config, str(item.get("link") or ""), reporter)
            if not args.dry_run
            else HatenaEntryDiscussion()
        )

        if args.no_llm or args.dry_run:
            summary = fallback_summary(item, page)
        else:
            try:
                ensure_usage_log_readable(destination)
            except OSError as exc:
                report(reporter, f"  vault unavailable before OpenAI request: {exc}")
                failed += 1
                break
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
                    max_article_chars=config.max_article_chars,
                )
            except Exception as exc:
                report(reporter, f"  summary warning: {exc}")
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
                    report(reporter, f"  rename warning: rename target already exists: {renamed_target}")
                    failed += 1
                    continue
                target = renamed_target
                rename_source = existing
        report(reporter, f"  note: {target}", verbose=True)

        canonical = canonical_item_from_metadata(item)
        has_comments = bool(page.discussion_text.strip() or public_comments.comments)
        comments_target = destination / comments_note_filename(target.name)
        comments_stem = comments_target.stem if has_comments else None

        markdown = render_note(
            item=item,
            page=page,
            summary=summary,
            base_tags=config.base_tags,
            generated_at=now,
            model=None if args.no_llm else model,
            comments_note=comments_stem,
        )
        comments_markdown = (
            render_comments_note(
                canonical,
                page,
                public_comments,
                main_note=target.name,
                generated_at=now,
            )
            if has_comments
            else None
        )
        usage_record: dict[str, Any] | None = None
        if isinstance(usage, dict):
            usage_record = build_usage_record(
                item=item,
                model=model,
                reasoning_effort=config.openai_reasoning_effort,
                max_output_tokens=config.max_output_tokens,
                usage=usage,
                price=usage_price,
                price_source=usage_price_source,
            )
            transaction = PendingTransaction(
                transaction_id=str(usage_record["transaction_id"]),
                target=target,
                markdown=markdown,
                usage_record=usage_record,
            )
            try:
                save_pending(destination, transaction, state_root=PENDING_STATE_ROOT)
            except OSError as exc:
                report(reporter, f"  pending recovery warning: {exc}")
                failed += 1
                break
        if args.dry_run:
            report(reporter, f"  dry-run: would write {len(markdown)} characters", verbose=True)
        else:
            try:
                if comments_markdown is not None:
                    write_note_atomically(comments_target, comments_markdown)
                write_note_atomically(target, markdown)
            except Exception as exc:
                report(reporter, f"  write warning: {exc}")
                failed += 1
                break
            if rename_source is not None:
                try:
                    old_comments = destination / comments_note_filename(rename_source.name)
                    rename_source.unlink()
                    if old_comments != comments_target and old_comments.exists():
                        old_comments.unlink()
                    renamed += 1
                except OSError as exc:
                    report(reporter, f"  rename warning: could not remove old note: {exc}")
                    failed += 1
            created += 1
            if usage_record is not None:
                try:
                    append_usage_record(
                        destination=destination,
                        record=usage_record,
                    )
                except OSError as exc:
                    report(reporter, f"  usage log warning: {exc}")
                    failed += 1
                    break
                try:
                    remove_pending(destination, state_root=PENDING_STATE_ROOT)
                except OSError as exc:
                    report(reporter, f"  pending recovery warning: {exc}")
                    failed += 1
                    break

        if config.sleep_seconds > 0:
            time.sleep(config.sleep_seconds)

    elapsed_seconds = time.perf_counter() - started_at
    report(
        reporter,
        f"done: processed={processed} created={created} renamed={renamed} skipped={skipped} "
        f"failed={failed} elapsed={elapsed_seconds:.1f}s"
    )
    return 1 if failed else 0


def _format_count_delta(current: int, previous: int | None) -> str:
    if previous is None:
        return ""
    delta = current - previous
    return " Δ±0" if delta == 0 else f" Δ{delta:+d}"


def process_hatena_export(
    config: Config,
    args: argparse.Namespace,
    reporter: ProgressReporter | None = None,
) -> int:
    started_at = time.perf_counter()
    location = (args.source_input or config.hatena_input).strip()
    hatena_id = ""
    hatena_api_key = ""
    if not location:
        hatena_id = require_env("HATENA_ID")
        hatena_api_key = require_env("HATENA_API_KEY")
    openai_key = "" if args.no_llm or args.dry_run else require_env("OPENAI_API_KEY")
    model = os.environ.get("OPENAI_MODEL", config.openai_model)
    usage_price: ModelPrice | None = None
    usage_price_source = "unavailable"
    if not args.no_llm and not args.dry_run:
        report(reporter, "usage: phase=refreshing-model-prices")
        price_refresh = refresh_model_prices(
            model,
            timeout_seconds=min(config.request_timeout_seconds, 10),
            include_recommended=False,
        )
        pricing_model = comparison_model(model)
        usage_price = next((price for price in price_refresh.prices if price.model == pricing_model), None)
        usage_price_source = "fallback" if pricing_model in price_refresh.fallback_models else price_refresh.source
        report(reporter, f"usage: price_source={usage_price_source}")
        if price_refresh.warning:
            report(reporter, f"usage: price_refresh_warning={price_refresh.warning}")

    destination = source_output_dir(config, "hatena")
    if not args.dry_run:
        destination.mkdir(parents=True, exist_ok=True)
        if not args.no_llm:
            recover_pending_transaction(destination)
    if args.dry_run:
        report(reporter, "dry-run: page fetching and OpenAI calls are disabled")

    if location:
        if reporter is not None:
            reporter.start_task("process: loading Hatena export", total=1)
        canonical_items = load_hatena_export(
            location,
            timeout_seconds=config.request_timeout_seconds,
            allow_private_urls=config.allow_private_urls,
        )
        if args.limit is not None:
            canonical_items = canonical_items[: max(0, args.limit)]
        if reporter is not None:
            reporter.advance()
    else:
        if reporter is not None:
            reporter.start_task("process: exporting Hatena bookmarks")
        reported_count = 0

        def on_page(collected: int, total: int) -> None:
            nonlocal reported_count
            if reporter is not None:
                if total > 0:
                    reporter.set_total(total)
                reporter.advance(max(0, collected - reported_count))
            reported_count = collected

        canonical_items = fetch_hatena_bookmarks(
            hatena_id,
            hatena_api_key,
            limit=args.limit,
            timeout_seconds=config.request_timeout_seconds,
            max_retries=config.max_retries,
            retry_base_seconds=config.retry_base_seconds,
            request_interval_seconds=config.hatena_request_interval_seconds,
            on_page=on_page,
        )
    if reporter is not None:
        reporter.start_task("process: generating Obsidian notes", total=len(canonical_items))

    processed = created = renamed = skipped = failed = 0
    now = datetime.now(timezone.utc).isoformat()
    for canonical in canonical_items:
        processed += 1
        if reporter is not None:
            reporter.advance()
        item = canonical.as_bookmark_metadata()
        existing_matches = sorted(destination.glob(f"* - {canonical.source_id}.md"))
        existing = preferred_note_path(existing_matches) if existing_matches else None
        target = existing or (destination / canonical_note_filename(canonical))
        if existing is not None and not args.force:
            if should_upgrade_note(existing, args):
                report(reporter, f"upgrade no-llm note: {existing}", verbose=True)
            else:
                report(reporter, f"skip existing: {existing}", verbose=True)
                skipped += 1
                continue

        report(reporter, f"process: {canonical.title or canonical.url}", verbose=True)
        page = PageFetchResult(url=canonical.url, text="", title="", error=None)
        if not args.dry_run and not args.skip_page_fetch and canonical.url:
            page = fetch_page_text(
                canonical.url,
                timeout_seconds=config.request_timeout_seconds,
                max_chars=config.max_article_chars,
                allow_private_urls=config.allow_private_urls,
            )
            if page.error:
                report(reporter, f"  page fetch warning: {page.error}", verbose=True)

        public_discussion = HatenaEntryDiscussion()
        if not args.dry_run:
            public_discussion = fetch_url_comments(config, canonical.url, reporter)

        if args.no_llm or args.dry_run:
            summary = fallback_summary(item, page)
        else:
            try:
                ensure_usage_log_readable(destination)
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
                    max_article_chars=config.max_article_chars,
                )
            except Exception as exc:
                report(reporter, f"  summary warning: {exc}")
                failed += 1
                continue
        usage = summary.pop(USAGE_FIELD, None)

        rename_source: Path | None = None
        desired = destination / canonical_note_filename(canonical, title=str(summary.get("note_title") or ""))
        if existing is None:
            target = desired
        elif args.rename_existing and desired != existing:
            if desired.exists():
                report(reporter, f"  rename warning: rename target already exists: {desired}")
                failed += 1
                continue
            target = desired
            rename_source = existing

        has_discussion = bool(page.discussion_text.strip() or public_discussion.comments)
        comments_target = destination / comments_note_filename(target.name)
        comments_stem = comments_target.stem if has_discussion else None
        markdown = render_canonical_note(
            item=canonical,
            page=page,
            summary=summary,
            base_tags=config.hatena_base_tags,
            generated_at=now,
            model=None if args.no_llm else model,
            comments_note=comments_stem,
        )
        comments_markdown = (
            render_comments_note(
                canonical,
                page,
                public_discussion,
                main_note=target.name,
                generated_at=now,
            )
            if has_discussion
            else None
        )
        usage_record: dict[str, Any] | None = None
        if isinstance(usage, dict):
            usage_record = build_usage_record(
                item=item,
                model=model,
                reasoning_effort=config.openai_reasoning_effort,
                max_output_tokens=config.max_output_tokens,
                usage=usage,
                price=usage_price,
                price_source=usage_price_source,
            )
            try:
                save_pending(
                    destination,
                    PendingTransaction(
                        transaction_id=str(usage_record["transaction_id"]),
                        target=target,
                        markdown=markdown,
                        usage_record=usage_record,
                    ),
                    state_root=PENDING_STATE_ROOT,
                )
            except OSError as exc:
                report(reporter, f"  pending recovery warning: {exc}")
                failed += 1
                break

        if args.dry_run:
            report(reporter, f"  dry-run: would write {len(markdown)} characters", verbose=True)
            continue
        try:
            if comments_markdown is not None:
                write_note_atomically(comments_target, comments_markdown)
            write_note_atomically(target, markdown)
            if rename_source is not None:
                old_comments = destination / comments_note_filename(rename_source.name)
                rename_source.unlink()
                if old_comments != comments_target and old_comments.exists():
                    old_comments.unlink()
                renamed += 1
            created += 1
            if usage_record is not None:
                append_usage_record(destination, usage_record)
                remove_pending(destination, state_root=PENDING_STATE_ROOT)
        except OSError as exc:
            report(reporter, f"  write warning: {exc}")
            failed += 1
            break
        item_interval = max(config.sleep_seconds, config.hatena_request_interval_seconds)
        if item_interval > 0:
            time.sleep(item_interval)

    elapsed_seconds = time.perf_counter() - started_at
    report(
        reporter,
        f"done: source=hatena processed={processed} created={created} renamed={renamed} "
        f"skipped={skipped} failed={failed} elapsed={elapsed_seconds:.1f}s",
    )
    return 1 if failed else 0


def sync_raindrop_summaries(
    config: Config,
    args: argparse.Namespace,
    client: RaindropClient | None,
    reporter: ProgressReporter | None = None,
) -> int:
    started_at = time.perf_counter()
    destination = output_dir(config)
    if not destination.exists():
        print(f"sync: no output folder: {destination}")
        return 0
    report(reporter, f"sync: request_interval={config.sync_request_interval_seconds:.1f}s")

    planned = 0
    updated = 0
    skipped = 0
    failed = 0
    note_paths = sorted(destination.glob("* - *.md"))
    if reporter is not None:
        reporter.start_task("sync: scanning Obsidian notes", total=len(note_paths))
    candidates, skipped = unique_note_candidates(
        note_paths,
        summary_sync_candidate,
        reporter.advance if reporter is not None else None,
    )
    if reporter is not None:
        reporter.start_task(
            "sync: updating Raindrop notes",
            total=min(len(candidates), args.limit) if args.limit is not None else len(candidates),
        )
    for note_path, candidate in tracked_items(candidates, reporter):
        raindrop_id, summary = candidate
        if args.limit is not None and planned >= args.limit:
            break
        planned += 1
        report(reporter, f"sync: {note_path.name} -> raindrop_id={raindrop_id}", verbose=True)
        if args.dry_run:
            report(reporter, "  dry-run: would update Raindrop note", verbose=True)
            continue
        if client is None:
            raise RuntimeError("Raindrop client is required when applying summary sync.")
        try:
            item = client.get_raindrop(raindrop_id)
            original_note = item.get("note")
            original_note = original_note if isinstance(original_note, str) else ""
            updated_note = upsert_raindrop_summary(original_note, summary)
            if updated_note == original_note:
                report(reporter, "  sync: unchanged", verbose=True)
                continue
            if len(updated_note) > 10_000:
                raise ValueError("updated Raindrop note exceeds the 10,000-character limit")
            client.update_raindrop_note(raindrop_id, updated_note)
            updated += 1
        except Exception as exc:
            report(reporter, f"  sync warning: {exc}")
            failed += 1
    elapsed_seconds = time.perf_counter() - started_at
    report(
        reporter,
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


def sync_raindrop_tags(
    config: Config,
    args: argparse.Namespace,
    client: RaindropClient | None,
    reporter: ProgressReporter | None = None,
) -> int:
    started_at = time.perf_counter()
    destination = output_dir(config)
    if not destination.exists():
        print(f"sync-tags: no output folder: {destination}")
        return 0
    report(reporter, f"sync-tags: request_interval={config.sync_request_interval_seconds:.1f}s")

    planned = 0
    updated = 0
    skipped = 0
    failed = 0
    note_paths = sorted(destination.glob("* - *.md"))
    if reporter is not None:
        reporter.start_task("sync-tags: scanning Obsidian notes", total=len(note_paths))
    candidates, skipped = unique_note_candidates(
        note_paths,
        lambda note_path: tag_sync_candidate(note_path, config.base_tags or []),
        reporter.advance if reporter is not None else None,
    )
    if reporter is not None:
        reporter.start_task(
            "sync-tags: updating Raindrop tags",
            total=min(len(candidates), args.limit) if args.limit is not None else len(candidates),
        )
    for note_path, candidate in tracked_items(candidates, reporter):
        raindrop_id, collection_id, note_tags = candidate
        if args.limit is not None and planned >= args.limit:
            break
        planned += 1
        if args.dry_run:
            report(
                reporter,
                f"sync-tags: {note_path.name} -> raindrop_id={raindrop_id} tags={note_tags}",
                verbose=True,
            )
            report(reporter, "  dry-run: would append Raindrop tags", verbose=True)
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
                report(reporter, f"sync-tags: {note_path.name} -> unchanged", verbose=True)
                continue
            report(
                reporter,
                f"sync-tags: {note_path.name} -> raindrop_id={raindrop_id} tags={missing_tags}",
                verbose=True,
            )
            client.append_raindrop_tags(collection_id, raindrop_id, missing_tags)
            updated += 1
        except Exception as exc:
            report(reporter, f"  sync-tags warning: {exc}")
            failed += 1
    elapsed_seconds = time.perf_counter() - started_at
    report(
        reporter,
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


def estimate_bookmarks(
    config: Config,
    args: argparse.Namespace,
    reporter: ProgressReporter | None = None,
) -> int:
    started_at = time.perf_counter()
    raindrop_token = require_env("RAINDROP_TOKEN")
    model = os.environ.get("OPENAI_MODEL", config.openai_model)
    _estimate_progress("phase=refreshing-model-prices", reporter)
    if reporter is not None:
        reporter.start_task("estimate: refreshing model prices", total=1)
    price_refresh = refresh_model_prices(model, timeout_seconds=min(config.request_timeout_seconds, 10))
    if reporter is not None:
        reporter.advance()
    report(reporter, f"estimate: price_source={price_refresh.source} url=https://developers.openai.com/api/docs/models")
    if price_refresh.warning:
        report(reporter, f"estimate: price_refresh_warning={price_refresh.warning}")
    client = RaindropClient(
        token=raindrop_token,
        timeout_seconds=config.request_timeout_seconds,
        max_retries=config.max_retries,
        retry_base_seconds=config.retry_base_seconds,
    )
    _estimate_progress("phase=collecting-bookmarks", reporter)
    if reporter is not None:
        reporter.start_task("estimate: collecting Raindrop bookmarks")
    items: list[dict[str, Any]] = []
    for item in client.iter_raindrops(
        collection_id=config.collection_id,
        per_page=config.per_page,
        nested=config.nested,
        limit=args.limit,
    ):
        items.append(item)
        if reporter is not None:
            reporter.advance()
        if len(items) % config.per_page == 0:
            _estimate_progress(f"collected={len(items)}", reporter)
    _estimate_progress(f"phase=sampling collected={len(items)}", reporter)
    population = len(items)
    if population == 0:
        report(reporter, "estimate: target=0")
        return 0
    sample_size = parse_sample_size(args.estimate_sample_size, population)
    if sample_size == 0:
        _estimate_progress("phase=calculating-costs count-only=true", reporter)
        _print_count_only_estimate(population, config.max_output_tokens, model, price_refresh.prices, reporter)
        _print_elapsed(started_at, reporter)
        return 0

    samples = select_sample(items, sample_size)
    _estimate_progress(f"phase=fetching-pages sample={len(samples)}", reporter)
    if reporter is not None:
        reporter.start_task("estimate: fetching sample pages", total=len(samples))
    token_counts: list[int] = []
    failures: Counter[str] = Counter()
    tokenizer_fallbacks: set[str] = set()
    for sample_index, item in enumerate(samples, start=1):
        report(
            reporter,
            f"estimate: fetching={sample_index}/{len(samples)} bookmark={_estimate_bookmark_label(item)}",
            verbose=True,
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
        prompt = build_prompt(
            item=item,
            page=page,
            language=config.language,
            max_article_chars=config.max_article_chars,
        )
        token_count, fallback = count_prompt_tokens(f"{SUMMARY_INSTRUCTIONS}\n\n{prompt}", model)
        token_counts.append(token_count)
        if fallback:
            tokenizer_fallbacks.add(fallback)
        if reporter is not None:
            reporter.advance()

    if not token_counts:
        report(reporter, f"estimate: target={population} sample={len(samples)} sampled=0 failed={sum(failures.values())}")
        for reason, count in failures.most_common():
            report(reporter, f"  page fetch failure: {count} x {reason}")
        _print_elapsed(started_at, reporter)
        return 1

    mean_tokens = sum(token_counts) / len(token_counts)
    projected_input_tokens = population * mean_tokens
    report(
        reporter,
        f"estimate: target={population} sample={len(samples)} sampled={len(token_counts)} "
        f"failed={sum(failures.values())}"
    )
    if args.skip_page_fetch:
        report(reporter, "estimate: page_fetch=skipped")
    report(
        reporter,
        f"estimate: mean_input_tokens={mean_tokens:.0f} "
        f"projected_input_tokens={projected_input_tokens:.0f} "
        f"max_output_tokens={config.max_output_tokens}"
    )
    for encoding_name in sorted(tokenizer_fallbacks):
        report(reporter, f"estimate: tokenizer fallback={encoding_name}")
    for reason, count in failures.most_common():
        report(reporter, f"  page fetch failure: {count} x {reason}")
    _estimate_progress("phase=calculating-costs", reporter)
    if reporter is not None:
        reporter.start_task("estimate: calculating costs", total=1)
    historical_usage = usage_output_ratio(
        output_dir(config),
        model,
        config.openai_reasoning_effort,
    )
    if historical_usage:
        output_ratio, usage_records = historical_usage
        typical_output_tokens = min(mean_tokens * output_ratio, config.max_output_tokens)
        report(
            reporter,
            f"estimate: assumed_output_tokens={typical_output_tokens:.0f} "
            f"(usage-ratio records={usage_records})"
        )
    else:
        typical_output_tokens = min(mean_tokens, config.max_output_tokens)
        report(reporter, f"estimate: assumed_output_tokens={typical_output_tokens:.0f} (input-matched)")
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
        report(reporter, line)
    if reporter is not None:
        reporter.advance()
    _print_elapsed(started_at, reporter)
    return 0


def _print_count_only_estimate(
    population: int,
    max_output_tokens: int,
    selected_model: str,
    prices: tuple[ModelPrice, ...],
    reporter: ProgressReporter | None = None,
) -> None:
    lower = projected_costs(population, 2_000, max_output_tokens, prices)
    upper = projected_costs(population, 10_000, max_output_tokens, prices)
    report(
        reporter,
        f"estimate: target={population} sample=0 count-only=true "
        "input_tokens_per_item=2000-10000 (generic range)"
    )
    report(reporter, f"estimate: max_output_tokens={max_output_tokens}")
    selected_pricing_model = comparison_model(selected_model)
    if not any(row.model == selected_pricing_model for row in lower):
        report(reporter, f"selected model: {selected_model} (not in comparison table)")
    report(reporter, "model\ttotal (max)")
    for lower_row, upper_row in zip(lower, upper):
        selected = " [selected]" if lower_row.model == selected_pricing_model else ""
        report(reporter, f"{lower_row.name}{selected}\t${lower_row.total_cost:.2f}-${upper_row.total_cost:.2f}")


def _print_elapsed(started_at: float, reporter: ProgressReporter | None = None) -> None:
    report(reporter, f"done: elapsed={time.perf_counter() - started_at:.1f}s")


def _estimate_progress(message: str, reporter: ProgressReporter | None = None) -> None:
    if reporter is not None and reporter.mode == "off":
        return
    report(reporter, f"estimate: {message}")


def _estimate_bookmark_label(item: dict[str, Any]) -> str:
    label = item.get("title") or item.get("link") or item.get("_id") or "(untitled)"
    return str(label).replace("\r", " ").replace("\n", " ")[:80]


def main(argv: list[str] | None = None) -> int:
    configure_console_output()
    command_args = argv if argv is not None else sys.argv[1:]
    if not command_args or command_args in (["-h"], ["--help"]) or is_modern_command(command_args):
        return modern_main(command_args)
    args = parse_args(command_args)
    try:
        load_env_file(Path(".env"))
        config = apply_overrides(load_config(args.config), args)
        sync_actions = int(args.sync_raindrop_summary) + int(args.sync_raindrop_tags)
        if sync_actions > 1:
            raise ValueError("Choose either --sync-raindrop-summary or --sync-raindrop-tags.")
        if args.estimate and (args.dry_run or args.list_collections or sync_actions):
            raise ValueError("--estimate cannot be combined with --dry-run, --list-collections, or sync options.")
        if args.source == "hatena" and (args.estimate or args.list_collections or sync_actions or args.collection is not None):
            raise ValueError("Hatena source does not support Raindrop collection, estimate, list, or sync options.")
        reporter = ProgressReporter(args.progress, verbose=args.verbose)
        if args.source == "hatena":
            with reporter:
                return process_hatena_export(config, args, reporter)
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
        with reporter:
            if args.estimate:
                return estimate_bookmarks(config, args, reporter)
            if args.sync_raindrop_summary:
                if args.dry_run:
                    return sync_raindrop_summaries(config, args, client=None, reporter=reporter)
                raindrop_token = require_env("RAINDROP_TOKEN")
                client = RaindropClient(
                    token=raindrop_token,
                    timeout_seconds=config.request_timeout_seconds,
                    max_retries=config.max_retries,
                    retry_base_seconds=config.retry_base_seconds,
                    request_interval_seconds=config.sync_request_interval_seconds,
                )
                return sync_raindrop_summaries(config, args, client, reporter)
            if args.sync_raindrop_tags:
                if args.dry_run:
                    return sync_raindrop_tags(config, args, client=None, reporter=reporter)
                raindrop_token = require_env("RAINDROP_TOKEN")
                client = RaindropClient(
                    token=raindrop_token,
                    timeout_seconds=config.request_timeout_seconds,
                    max_retries=config.max_retries,
                    retry_base_seconds=config.retry_base_seconds,
                    request_interval_seconds=config.sync_request_interval_seconds,
                )
                return sync_raindrop_tags(config, args, client, reporter)
            return process_bookmarks(config, args, reporter)
    except Exception as exc:
        print_cli_error(exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
