from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Callable, Iterable

from .canonical import CanonicalItem, canonical_item_from_metadata
from .assets import fetch_image
from .extract import PageFetchResult, decode_html, extract_content_images, fetch_page_text
from .hatena import HatenaEntryDiscussion, fetch_hatena_bookmarks, fetch_hatena_entry_discussion
from .raindrop import RaindropClient
from .rss import fetch_rss_items
from .store import VaultStore, sha256_bytes, stable_json
from .vault import VaultConfig


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
    progress: Callable[[int, CanonicalItem], None] | None = None,
) -> SyncReport:
    providers = _selected_providers(config, source)
    fingerprint = sha256_bytes(
        stable_json(
            {
                "providers": providers,
                "limit": limit,
                "fetch_pages": fetch_pages,
                "fetch_comments": fetch_comments,
                "force_fetch": force_fetch,
                "config": config.fetch,
            }
        ).encode("utf-8")
    )
    run_id = store.create_sync_run(providers, fingerprint)
    processed = changed = failed = fetched = 0
    try:
        for provider in providers:
            for item, raw_payload in _provider_items(config, provider, limit):
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
                        _store_page(store, stored.resource_id, page, config)
                        fetched += 1
                    if stored.resource_id and fetch_comments and item.url:
                        _store_hatena_comments(store, stored.resource_id, item.url)
                    store.record_sync_item(run_id, stored.source_item_id, "completed")
                    changed += int(stored.changed)
                except Exception as exc:
                    failed += 1
                    store.record_sync_item(run_id, stored.source_item_id, "failed", str(exc))
                finally:
                    if progress is not None:
                        progress(processed, item)
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


def _provider_items(config: VaultConfig, provider: str, limit: int | None) -> Iterable[tuple[CanonicalItem, bytes]]:
    if provider == "raindrop":
        token = _required_env("RAINDROP_TOKEN")
        settings = config.providers[provider]
        collection_id = settings.collection_id if settings.collection_id is not None else 0
        client = RaindropClient(token=token)
        for raw in client.iter_raindrops(collection_id=collection_id, per_page=50, nested=True, limit=limit):
            yield canonical_item_from_metadata(raw), stable_json(raw).encode("utf-8")
        return
    if provider == "hatena":
        items = fetch_hatena_bookmarks(
            _required_env("HATENA_ID"),
            _required_env("HATENA_API_KEY"),
            limit=limit,
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


def _store_page(store: VaultStore, resource_id: str, page: PageFetchResult, config: VaultConfig) -> None:
    http_payload_id = None
    if page.raw_body is not None:
        http_payload_id = store.put_payload(
            page.raw_body,
            media_type=page.media_type or "application/octet-stream",
            charset=page.content_encoding,
            source_url=page.final_url or page.url,
            headers=page.response_headers,
        )
    rendered_payload_id = None
    if page.rendered_html:
        rendered_payload_id = store.put_payload(
            page.rendered_html.encode("utf-8"),
            media_type="text/html; charset=utf-8",
            charset="utf-8",
            source_url=page.final_url or page.url,
        )
    resource_revision_id, _ = store.record_resource_revision(
        resource_id,
        content_markdown=page.text,
        discussion_text=page.discussion_text,
        title=page.title,
        final_url=page.final_url or page.url,
        extracted_by=f"{page.fetch_method}:{page.extraction_method}".strip(":"),
        http_payload_id=http_payload_id,
        rendered_payload_id=rendered_payload_id,
        content_truncated=page.content_truncated,
        warning=page.error,
    )
    html = page.rendered_html
    if not html and page.raw_body is not None and "html" in page.media_type.lower():
        _, html = decode_html(page.raw_body, page.media_type)
    if not html:
        return
    max_bytes = int(config.fetch.get("document_max_bytes", 100 * 1024 * 1024))
    for image in extract_content_images(html, page.final_url or page.url):
        downloaded = fetch_image(
            image.url,
            max_bytes=max_bytes,
            allow_private_urls=bool(config.fetch.get("allow_private_hosts")),
        )
        if downloaded.body is None:
            continue
        store.put_asset(
            resource_id=resource_id,
            resource_revision_id=resource_revision_id,
            content=downloaded.body,
            media_type=downloaded.media_type,
            source_url=downloaded.final_url or image.url,
            alt_text=image.alt_text,
            headers=downloaded.headers,
        )


def _store_hatena_comments(store: VaultStore, resource_id: str, url: str) -> None:
    discussion = fetch_hatena_entry_discussion(url)
    for comment in discussion.comments:
        if not comment.user:
            continue
        store.upsert_comment(
            provider="hatena",
            resource_id=resource_id,
            author=comment.user,
            body=comment.comment,
            tags=comment.tags,
            metadata={
                "timestamp": comment.timestamp,
                "entry_url": discussion.entry_url,
                "entry_id": discussion.entry_id,
                "star_url": comment.star_url,
                "bookmark_count": discussion.bookmark_count,
            },
        )


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value
