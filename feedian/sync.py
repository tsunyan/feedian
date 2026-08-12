from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Callable, Iterable

from .canonical import CanonicalItem, canonical_item_from_metadata
from .extract import PageFetchResult, decode_html, extract_content_images, fetch_page_text
from .hatena import (
    HatenaEntryDiscussion,
    HatenaPublicComment,
    fetch_hatena_bookmark_counts,
    fetch_hatena_bookmarks,
    fetch_hatena_entry_discussion,
    fetch_hatena_star_counts,
    hatena_comment_star_url,
)
from .raindrop import RaindropClient
from .rss import fetch_rss_items
from .store import VaultStore, sha256_bytes, stable_json
from .vault import VaultConfig


HATENA_COMMENT_LIMIT = 20


@dataclass(frozen=True)
class SyncReport:
    run_id: str
    processed: int
    changed: int
    failed: int
    fetched: int


def sync_vault(
    store: VaultStore,
    config: VaultConfig,
    *,
    source: str = "all",
    limit: int | None = None,
    fetch_pages: bool = True,
    fetch_comments: bool = True,
    force_fetch: bool = False,
    force_comments: bool = False,
    progress: Callable[[int, CanonicalItem], None] | None = None,
    collection_progress: Callable[[str, int, int], None] | None = None,
    comment_progress: Callable[[int, int], None] | None = None,
) -> SyncReport:
    store.fail_interrupted_sync_runs()
    providers = _selected_providers(config, source)
    fingerprint = sha256_bytes(
        stable_json(
            {
                "providers": providers,
                "limit": limit,
                "fetch_pages": fetch_pages,
                "fetch_comments": fetch_comments,
                "force_fetch": force_fetch,
                "force_comments": force_comments,
                "config": config.fetch,
            }
        ).encode("utf-8")
    )
    run_id = store.create_sync_run(providers, fingerprint)
    processed = changed = failed = fetched = 0
    comment_targets: dict[str, tuple[str, str, str]] = {}
    try:
        for provider in providers:
            for item, raw_payload in _provider_items(
                config, provider, limit, collection_progress=collection_progress
            ):
                processed += 1
                stored = store.upsert_canonical_item(item, source_payload=raw_payload)
                try:
                    if stored.resource_id and fetch_pages and item.url and store.should_fetch_resource(
                        stored.resource_id, refresh_days=int(config.fetch.get("refresh_days", 30)), force=force_fetch
                    ):
                        page = fetch_page_text(
                            item.url,
                            timeout_seconds=30,
                            max_chars=10_000,
                            allow_private_urls=False,
                        )
                        _store_page(store, stored.resource_id, page)
                        fetched += 1
                    if stored.resource_id and fetch_comments and item.url:
                        comment_targets.setdefault(
                            stored.resource_id, (stored.source_item_id, stored.resource_id, item.url)
                        )
                    store.record_sync_item(run_id, stored.source_item_id, "completed")
                    changed += int(stored.changed)
                except Exception as exc:
                    failed += 1
                    store.record_sync_item(run_id, stored.source_item_id, "failed", str(exc))
                finally:
                    if progress is not None:
                        progress(processed, item)
        failed += _store_hatena_comments_parallel(
            store,
            run_id,
            list(comment_targets.values()),
            workers=int(config.fetch.get("comment_workers", 8)),
            force=force_comments,
            progress=comment_progress,
        )
        store.finish_sync_run(run_id, status="partial" if failed else "completed")
    except Exception as exc:
        store.finish_sync_run(run_id, status="failed", error=str(exc))
        raise
    return SyncReport(run_id=run_id, processed=processed, changed=changed, failed=failed, fetched=fetched)


def _selected_providers(config: VaultConfig, source: str) -> list[str]:
    if source == "all":
        return [name for name, settings in config.providers.items() if settings.enabled]
    if source not in config.providers:
        raise ValueError(f"Unknown provider: {source}")
    return [source]


def _provider_items(
    config: VaultConfig,
    provider: str,
    limit: int | None,
    *,
    collection_progress: Callable[[str, int, int], None] | None = None,
) -> Iterable[tuple[CanonicalItem, bytes]]:
    if provider == "raindrop":
        token = _required_env("RAINDROP_TOKEN")
        settings = config.providers[provider]
        collection_id = settings.collection_id if settings.collection_id is not None else 0
        client = RaindropClient(token=token)
        for raw in client.iter_raindrops(collection_id=collection_id, per_page=50, nested=True, limit=limit):
            yield canonical_item_from_metadata(raw), stable_json(raw).encode("utf-8")
        return
    if provider == "hatena":
        if collection_progress is not None:
            collection_progress(provider, 0, 0)
        items = fetch_hatena_bookmarks(
            _required_env("HATENA_ID"),
            _required_env("HATENA_API_KEY"),
            limit=limit,
            on_page=(
                (lambda collected, total: collection_progress(provider, collected, total))
                if collection_progress is not None
                else None
            ),
        )
        for item in items:
            yield item, stable_json(item.as_bookmark_metadata()).encode("utf-8")
        return
    if provider == "rss":
        feeds = config.providers[provider].feeds
        if not feeds:
            raise ValueError("RSS is enabled but providers.rss.feeds is empty.")
        count = 0
        for feed_url in feeds:
            for entry in fetch_rss_items(feed_url):
                if limit is not None and count >= limit:
                    return
                count += 1
                yield entry.item, entry.payload
        return
    raise ValueError(f"Unsupported provider: {provider}")


def _store_page(store: VaultStore, resource_id: str, page: PageFetchResult) -> None:
    http_payload_id = None
    if page.raw_body is not None and "html" not in (page.media_type or "").lower():
        http_payload_id = store.put_payload(
            page.raw_body,
            media_type=page.media_type or "application/octet-stream",
            charset=page.content_encoding,
            source_url=page.final_url or page.url,
            headers=page.response_headers,
        )
    resource_revision_id, _ = store.record_resource_revision(
        resource_id,
        content_markdown=page.text,
        discussion_text=page.discussion_text,
        title=page.title,
        final_url=page.final_url or page.url,
        extracted_by=f"{page.fetch_method}:{page.extraction_method}".strip(":"),
        http_payload_id=http_payload_id,
        rendered_payload_id=None,
        content_truncated=page.content_truncated,
        warning=page.error,
    )
    html = page.rendered_html
    if not html and page.raw_body is not None and "html" in page.media_type.lower():
        _, html = decode_html(page.raw_body, page.media_type)
    if not html:
        return
    images = extract_content_images(html, page.final_url or page.url)
    store.replace_resource_images(
        resource_id=resource_id,
        resource_revision_id=resource_revision_id,
        images=[(image.url, image.alt_text) for image in images],
    )


def _store_hatena_discussion(store: VaultStore, resource_id: str, discussion: HatenaEntryDiscussion) -> None:
    store.upsert_comments(
        provider="hatena",
        resource_id=resource_id,
        comments=[
            {
                "author": comment.user,
                "body": comment.comment,
                "tags": comment.tags,
                "posted_at": comment.timestamp,
                "star_count": comment.star_count,
            }
            for comment in discussion.comments
            if comment.user
        ],
        replace=True,
    )
    store.update_comment_state(
        resource_id,
        discussion.bookmark_count,
        provider="hatena",
        entry_url=discussion.entry_url,
        entry_id=discussion.entry_id,
    )


def _select_hatena_comments(
    discussion: HatenaEntryDiscussion,
    *,
    limit: int = HATENA_COMMENT_LIMIT,
    workers: int = 4,
) -> list[HatenaPublicComment]:
    candidates = [comment for comment in discussion.comments if comment.user]
    star_urls = [
        hatena_comment_star_url(comment.user, comment.timestamp, discussion.entry_id)
        for comment in candidates
    ]
    counts = fetch_hatena_star_counts(star_urls, workers=max(1, workers))
    enriched = [
        replace(comment, star_count=counts.get(star_url, 0) if star_url else 0)
        for comment, star_url in zip(candidates, star_urls)
    ]
    enriched.sort(
        key=lambda comment: (
            -(comment.star_count or 0),
            not bool(comment.timestamp),
            comment.timestamp,
            comment.user,
        )
    )
    return enriched[: max(0, limit)]


def _store_hatena_comments_parallel(
    store: VaultStore,
    run_id: str,
    targets: list[tuple[str, str, str]],
    *,
    workers: int,
    force: bool = False,
    progress: Callable[[int, int], None] | None = None,
) -> int:
    if not targets:
        return 0

    total = len(targets)
    if progress is not None:
        progress(0, total)

    try:
        counts = fetch_hatena_bookmark_counts(
            [url for _, _, url in targets], batch_size=20, workers=min(max(1, workers), 4)
        )
    except Exception:
        # Count lookup is an optimization. A failure falls back to the complete,
        # authoritative entry response rather than losing comments.
        counts = {}

    due_targets = []
    for target in targets:
        _, resource_id, url = target
        current_count = counts.get(url)
        previous_count = store.comment_bookmark_count(resource_id, provider="hatena")
        if not force and current_count is not None and previous_count == current_count:
            continue
        due_targets.append(target)
    if not due_targets:
        if progress is not None:
            progress(total, total)
        return 0

    skipped = total - len(due_targets)
    if progress is not None and skipped:
        progress(skipped, total)

    def fetch_target(target: tuple[str, str, str]):
        source_item_id, resource_id, url = target
        try:
            discussion = fetch_hatena_entry_discussion(url)
            selected = _select_hatena_comments(discussion, workers=1)
            return source_item_id, resource_id, replace(discussion, comments=selected), None
        except Exception as exc:
            return source_item_id, resource_id, None, exc

    failures = 0
    processed = skipped
    worker_count = max(1, min(len(due_targets), workers))
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="feedian-comments") as executor:
        results = executor.map(fetch_target, due_targets)
        for source_item_id, resource_id, discussion, error in results:
            if error is not None or discussion is None:
                failures += 1
                store.record_sync_item(run_id, source_item_id, "failed", str(error))
            else:
                _store_hatena_discussion(store, resource_id, discussion)
            processed += 1
            if progress is not None:
                progress(processed, total)
    return failures


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value
