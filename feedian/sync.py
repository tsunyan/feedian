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
from .rss import RssItem, fetch_rss_items, published_timestamp
from .store import VaultStore, sha256_bytes, stable_json
from .vault import FetchRetrySettings, VaultConfig, fetch_retry_settings, normalized_rss_feeds


HATENA_COMMENT_LIMIT = 20


@dataclass(frozen=True)
class SyncReport:
    run_id: str
    processed: int
    changed: int
    failed: int
    fetched: int
    quick: bool = False
    skipped: int = 0
    retried: int = 0
    stopped_early: tuple[str, ...] = ()


def sync_vault(
    store: VaultStore,
    config: VaultConfig,
    *,
    source: str = "all",
    limit: int | None = None,
    quick: bool = False,
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
    stop_after_known_pages = 1
    if quick:
        stop_after_known_pages = config.fetch.get("quick_stop_after_known_pages", 1)
        # Coercing first would silently turn 1.5 into 1 and true into 1, cutting
        # collection shorter than the value the user wrote.
        if isinstance(stop_after_known_pages, bool) or not isinstance(stop_after_known_pages, int):
            raise ValueError("config.fetch.quick_stop_after_known_pages must be an integer")
        if stop_after_known_pages < 1:
            raise ValueError("config.fetch.quick_stop_after_known_pages must be >= 1")
    refresh_days = int(config.fetch.get("refresh_days", 30))
    retry_settings = fetch_retry_settings(config)
    fingerprint = sha256_bytes(
        stable_json(
            {
                "providers": providers,
                "limit": limit,
                "quick": quick,
                "fetch_pages": fetch_pages,
                "fetch_comments": fetch_comments,
                "force_fetch": force_fetch,
                "force_comments": force_comments,
                "config": config.fetch,
            }
        ).encode("utf-8")
    )
    run_id = store.create_sync_run(providers, fingerprint, mode="quick" if quick else "full")
    processed = changed = failed = fetched = skipped = retried = 0
    comment_targets: dict[str, tuple[str, str, str]] = {}
    provider_errors: list[str] = []
    stopped_early_providers: list[str] = []
    handled_resource_ids: set[str] = set()

    def record_provider_error(provider: str, source_name: str, error: Exception) -> None:
        nonlocal failed
        failed += 1
        provider_errors.append(f"{provider} {source_name}: {error}")

    try:
        for provider in providers:
            known = store.known_native_ids(provider) if quick else None
            provider_fetch_attempts = 0
            for item, raw_payload in _provider_items(
                config,
                provider,
                limit,
                collection_progress=collection_progress,
                store=store,
                provider_error=record_provider_error,
                quick=quick,
                known=known,
                stop_after_known_pages=stop_after_known_pages,
                on_stopped_early=stopped_early_providers.append,
            ):
                if quick and known is not None and item.source_id in known:
                    skipped += 1
                    if progress is not None:
                        # Count skips too, so the collection-sized progress bar advances.
                        progress(processed + skipped, item)
                    continue
                processed += 1
                stored = store.upsert_canonical_item(item, source_payload=raw_payload)
                if quick and stored.resource_id:
                    handled_resource_ids.add(stored.resource_id)
                # Bound before the try: should_fetch_resource reads fetch_capture and
                # parses its timestamp, so it can raise, and the handler below reads
                # this to decide whose audit rows to write.
                should_fetch_page = False
                try:
                    should_fetch_page = bool(
                        stored.resource_id
                        and fetch_pages
                        and item.url
                        and store.should_fetch_resource(
                            stored.resource_id,
                            refresh_days=refresh_days,
                            force=force_fetch,
                            retry_base_minutes=retry_settings.retry_base_minutes,
                            retry_max_days=retry_settings.retry_max_days,
                            terminal_http_statuses=retry_settings.terminal_http_statuses,
                            terminal_failure_kinds=retry_settings.terminal_failure_kinds,
                            terminal_kind_failures=retry_settings.terminal_kind_failures,
                        )
                    )
                    if should_fetch_page and stored.resource_id:
                        provider_fetch_attempts += 1
                        try:
                            etag, last_modified = store.resource_fetch_validators(stored.resource_id)
                            page = fetch_page_text(
                                item.url,
                                timeout_seconds=retry_settings.timeout_seconds,
                                max_chars=10_000,
                                allow_private_urls=False,
                                etag=etag,
                                last_modified=last_modified,
                                browser_timeout_seconds=retry_settings.browser_timeout_seconds,
                            )
                        except Exception as exc:
                            if item.embedded_content and not _resource_has_revision(store, stored.resource_id):
                                store.record_resource_revision(
                                    stored.resource_id,
                                    content_markdown=item.embedded_content,
                                    title=item.title,
                                    final_url=item.url,
                                    extracted_by="rss-feed-fallback",
                                )
                            elif stored.resource_id:
                                # Only when the feed body did not stand in: record_failed_fetch
                                # rewrites the capture, and its defaults would clear the payload
                                # and final URL that the fallback revision just recorded.
                                store.record_failed_fetch(stored.resource_id, warning=str(exc))
                            raise
                        if page.not_modified:
                            store.record_not_modified_fetch(
                                stored.resource_id,
                                final_url=page.final_url or item.url,
                                response_headers=page.response_headers,
                            )
                        elif not page.text.strip() and item.embedded_content:
                            page.text = item.embedded_content
                            page.title = page.title or item.title
                            page.extraction_method = "rss-feed-fallback"
                        if not page.not_modified:
                            _store_page(store, stored.resource_id, page)
                        fetched += 1
                    elif stored.resource_id and item.embedded_content and not _resource_has_revision(store, stored.resource_id):
                        store.record_resource_revision(
                            stored.resource_id,
                            content_markdown=item.embedded_content,
                            title=item.title,
                            final_url=item.url,
                            extracted_by="rss-feed",
                        )
                    if stored.resource_id and fetch_comments and item.url:
                        comment_targets.setdefault(
                            stored.resource_id, (stored.source_item_id, stored.resource_id, item.url)
                        )
                    for source_item_id in _audited_source_items(
                        store, stored, providers, quick=quick, fetched_body=should_fetch_page
                    ):
                        store.record_sync_item(run_id, source_item_id, "completed")
                    changed += int(stored.changed)
                except Exception as exc:
                    failed += 1
                    for source_item_id in _audited_source_items(
                        store, stored, providers, quick=quick, fetched_body=should_fetch_page
                    ):
                        store.record_sync_item(run_id, source_item_id, "failed", str(exc))
                finally:
                    if progress is not None:
                        progress(processed + skipped, item)
            if quick and fetch_pages:
                budget = None if limit is None else max(0, limit - provider_fetch_attempts)
                provider_retried, provider_fetched, provider_failed = _run_quick_body_only_pass(
                    store,
                    run_id,
                    provider,
                    all_providers=providers,
                    budget=budget,
                    handled_resource_ids=handled_resource_ids,
                    refresh_days=refresh_days,
                    force_fetch=force_fetch,
                    retry_settings=retry_settings,
                )
                retried += provider_retried
                fetched += provider_fetched
                failed += provider_failed
        failed += _store_hatena_comments_parallel(
            store,
            run_id,
            list(comment_targets.values()),
            workers=int(config.fetch.get("comment_workers", 8)),
            force=force_comments,
            progress=comment_progress,
        )
        store.finish_sync_run(
            run_id,
            status="partial" if failed else "completed",
            error="; ".join(provider_errors) if provider_errors else None,
        )
    except Exception as exc:
        store.finish_sync_run(run_id, status="failed", error=str(exc))
        raise
    return SyncReport(
        run_id=run_id,
        processed=processed,
        changed=changed,
        failed=failed,
        fetched=fetched,
        quick=quick,
        skipped=skipped,
        retried=retried,
        stopped_early=tuple(stopped_early_providers),
    )


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
    store: VaultStore | None = None,
    provider_error: Callable[[str, str, Exception], None] | None = None,
    quick: bool = False,
    known: set[str] | None = None,
    stop_after_known_pages: int = 1,
    on_stopped_early: Callable[[str], None] | None = None,
) -> Iterable[tuple[CanonicalItem, bytes]]:
    if provider == "raindrop":
        token = _required_env("RAINDROP_TOKEN")
        settings = config.providers[provider]
        collection_id = settings.collection_id if settings.collection_id is not None else 0
        client = RaindropClient(token=token)
        if quick:
            per_page = 50
            known_ids = known or set()
            consecutive_known_pages = 0
            new_items = 0
            for page_items in client.iter_raindrop_pages(collection_id=collection_id, per_page=per_page, nested=True):
                page_all_known = True
                for raw in page_items:
                    item = canonical_item_from_metadata(raw)
                    yield item, stable_json(raw).encode("utf-8")
                    if item.source_id not in known_ids:
                        page_all_known = False
                        # limit counts the items quick actually takes on, matching
                        # "maximum items per provider" in full mode. Counting every
                        # examined item instead would cut collection short before
                        # reaching new items that sort below known ones.
                        new_items += 1
                        if limit is not None and new_items >= limit:
                            return
                if len(page_items) < per_page:
                    return
                consecutive_known_pages = consecutive_known_pages + 1 if page_all_known else 0
                if consecutive_known_pages >= stop_after_known_pages:
                    if on_stopped_early is not None:
                        on_stopped_early(provider)
                    return
            return
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
            known=known if quick else None,
            # Zero keeps full mode sweeping every page; the caller validated the
            # configured value before quick ever reaches here.
            stop_after_known_pages=stop_after_known_pages if quick else 0,
            on_stopped_early=(
                (lambda: on_stopped_early(provider)) if on_stopped_early is not None else None
            ),
        )
        for item in items:
            yield item, stable_json(item.as_bookmark_metadata()).encode("utf-8")
        return
    if provider == "rss":
        settings = config.providers[provider]
        feeds = [feed for feed in normalized_rss_feeds(settings) if feed.enabled]
        if not feeds:
            raise ValueError("RSS is enabled but providers.rss.feeds is empty.")
        collected: list[tuple[int, RssItem]] = []
        seen_source_ids: set[str] = set()
        sequence = 0
        for feed in feeds:
            persisted = _existing_rss_feed_metadata(store, feed.url) if store is not None else {}
            try:
                entries = fetch_rss_items(
                    feed.url,
                    name=feed.name,
                    folder=feed.folder or str(persisted.get("feed_folder") or ""),
                    tags=feed.tags,
                    route=feed.route,
                    category_routes=settings.category_routes,
                    etag=str(persisted.get("feed_etag") or ""),
                    last_modified=str(persisted.get("feed_last_modified") or ""),
                )
            except Exception as exc:
                if provider_error is None:
                    raise
                provider_error(provider, feed.name or feed.url, exc)
                continue
            for entry in entries:
                if entry.item.source_id in seen_source_ids:
                    continue
                seen_source_ids.add(entry.item.source_id)
                collected.append((sequence, entry))
                sequence += 1
        collected.sort(
            key=lambda value: (
                published_timestamp(value[1]) is not None,
                published_timestamp(value[1]) or 0,
                -value[0],
            ),
            reverse=True,
        )
        selected = collected if limit is None else collected[: max(0, limit)]
        for _, entry in selected:
            yield entry.item, entry.payload
        return
    raise ValueError(f"Unsupported provider: {provider}")


def _put_page_payload(store: VaultStore, page: PageFetchResult) -> str | None:
    if page.raw_body is not None and "html" not in (page.media_type or "").lower():
        return store.put_payload(
            page.raw_body,
            media_type=page.media_type or "application/octet-stream",
            charset=page.content_encoding,
            source_url=page.final_url or page.url,
            headers=page.response_headers,
        )
    return None


def _store_page(store: VaultStore, resource_id: str, page: PageFetchResult) -> None:
    http_payload_id = _put_page_payload(store, page)
    if page.error and not page.text.strip():
        store.record_failed_fetch(
            resource_id,
            warning=page.error,
            final_url=page.final_url or page.url,
            http_payload_id=http_payload_id,
            rendered_payload_id=None,
            response_headers=page.response_headers,
            http_status=page.http_status,
            failure_kind=page.failure_kind,
        )
        return
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
        response_headers=page.response_headers,
        http_status=page.http_status,
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


def _run_quick_body_only_pass(
    store: VaultStore,
    run_id: str,
    provider: str,
    *,
    all_providers: list[str],
    budget: int | None,
    handled_resource_ids: set[str],
    refresh_days: int,
    force_fetch: bool,
    retry_settings: FetchRetrySettings,
) -> tuple[int, int, int]:
    """Retry resources that still have no body, without touching any provider.

    This is the (B1) pass: a DB-driven retry over `unfetched_resources` that
    issues no provider request at all. Returns (retried, fetched, failed).
    """
    retried = fetched = failed = 0
    remaining = budget
    for resource_id, url in store.unfetched_resources([provider]):
        if resource_id in handled_resource_ids:
            continue
        if remaining is not None and remaining <= 0:
            break
        if not store.should_fetch_resource(
            resource_id,
            refresh_days=refresh_days,
            force=force_fetch,
            retry_base_minutes=retry_settings.retry_base_minutes,
            retry_max_days=retry_settings.retry_max_days,
            terminal_http_statuses=retry_settings.terminal_http_statuses,
            terminal_failure_kinds=retry_settings.terminal_failure_kinds,
            terminal_kind_failures=retry_settings.terminal_kind_failures,
        ):
            continue
        handled_resource_ids.add(resource_id)
        if remaining is not None:
            remaining -= 1
        # Scoped to every selected provider, not just this pass's own. One resource
        # is often shared (the same URL bookmarked in Raindrop and in Hatena), and it
        # leaves `unfetched_resources` as soon as this fetch lands, so the other
        # provider's pass would never get to record its outcome.
        source_item_ids = store.source_items_for_resource(resource_id, all_providers)
        try:
            etag, last_modified = store.resource_fetch_validators(resource_id)
            page = fetch_page_text(
                url,
                timeout_seconds=retry_settings.timeout_seconds,
                max_chars=10_000,
                allow_private_urls=False,
                etag=etag,
                last_modified=last_modified,
                browser_timeout_seconds=retry_settings.browser_timeout_seconds,
            )
            if page.not_modified:
                store.record_not_modified_fetch(
                    resource_id,
                    final_url=page.final_url or url,
                    response_headers=page.response_headers,
                )
            else:
                _store_page(store, resource_id, page)
            fetched += 1
            # retried answers "is the backlog draining?", so only a resource that
            # came away with a body counts. fetched stays an attempt count, as it
            # is in the item loop.
            if page.text.strip():
                retried += 1
            for source_item_id in source_item_ids:
                store.record_sync_item(run_id, source_item_id, "completed")
        except Exception as exc:
            failed += 1
            try:
                # Without a capture the resource keeps a NULL fetched_at, so the
                # oldest-attempt-first ordering hands it the budget again on every
                # run and the resources behind it never get a turn.
                store.record_failed_fetch(resource_id, warning=str(exc))
            except Exception:
                pass
            for source_item_id in source_item_ids:
                store.record_sync_item(run_id, source_item_id, "failed", str(exc))
    return retried, fetched, failed


def _audited_source_items(
    store: VaultStore, stored, providers: list[str], *, quick: bool, fetched_body: bool
) -> list[str]:
    """Source items this item's outcome should be recorded against.

    Normally just the item's own. But when quick fetches a body for a resource
    that other source items share, those items are skipped as known and the
    body-only pass can no longer reach them -- the resource has a body now. The
    fetch happened on the resource's behalf, so every reference gets the result;
    recording only the one that triggered it is the representative-single-item
    audit the specification rejected.
    """
    if not quick or not fetched_body or not stored.resource_id:
        return [stored.source_item_id]
    shared = store.source_items_for_resource(stored.resource_id, providers)
    return shared or [stored.source_item_id]


def _resource_has_revision(store: VaultStore, resource_id: str) -> bool:
    row = store.connection.execute(
        "SELECT current_revision_id FROM resource WHERE resource_id = ?", (resource_id,)
    ).fetchone()
    return bool(row is not None and row["current_revision_id"])


def _existing_rss_feed_metadata(store: VaultStore, feed_url: str) -> dict[str, object]:
    rows = store.connection.execute(
        """
        SELECT sr.metadata_json
        FROM source_item AS s
        JOIN source_item_revision AS sr ON sr.source_revision_id = s.current_revision_id
        WHERE s.provider = 'rss' AND sr.metadata_json LIKE ?
        ORDER BY s.updated_at DESC
        """,
        (f'%"feed_url":"{feed_url.replace(chr(34), "")}"%',),
    ).fetchall()
    for row in rows:
        try:
            metadata = json.loads(str(row["metadata_json"]))
        except json.JSONDecodeError:
            continue
        provider_metadata = metadata.get("_feedian_provider_metadata") or {}
        if provider_metadata.get("feed_url") == feed_url:
            return dict(provider_metadata)
    return {}


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
