from __future__ import annotations

from feedian.canonical import CanonicalItem
from feedian.extract import PageFetchResult
from feedian.hatena import HatenaEntryDiscussion, HatenaPublicComment
from feedian.store import VaultStore
from feedian.sync import sync_vault
from feedian.vault import VaultConfig


def test_sync_stores_provider_page_and_hatena_comments(monkeypatch, tmp_path) -> None:
    item = CanonicalItem(source="hatena", source_id="hatena-1", content_key="url:one", url="https://example.test/a", title="A")
    monkeypatch.setattr(
        "feedian.sync._provider_items",
        lambda config, provider, limit: iter([(item, b'{"bookmark": 1}')]),
    )
    monkeypatch.setattr(
        "feedian.sync.fetch_page_text",
        lambda *args, **kwargs: PageFetchResult(
            url=item.url,
            final_url=item.url,
            text="article body",
            title="Article",
            raw_body=b"<html>article body</html>",
            media_type="text/html",
            response_headers={"ETag": "one"},
        ),
    )
    monkeypatch.setattr(
        "feedian.sync.fetch_hatena_entry_discussion",
        lambda url: HatenaEntryDiscussion(
            entry_url=url,
            bookmark_count=1,
            comments=[HatenaPublicComment(user="alice", comment="useful", tags=["tech"], timestamp="2026-08-11")],
        ),
    )
    config = VaultConfig(providers={"hatena": VaultConfig().providers["hatena"]})
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        report = sync_vault(store, config, source="hatena")

        assert report.processed == 1
        assert report.fetched == 1
        assert report.failed == 0
        counts = store.status_counts()
        assert counts["resource"] == 1
        assert counts["resource_revision"] == 1
        assert counts["comment"] == 1
        assert counts["payload"] == 2
        assert store.latest_sync_run()["status"] == "completed"
    finally:
        store.close()


def test_sync_skips_recent_page_fetches(monkeypatch, tmp_path) -> None:
    item = CanonicalItem(source="hatena", source_id="hatena-1", content_key="url:one", url="https://example.test/a", title="A")
    monkeypatch.setattr("feedian.sync._provider_items", lambda *_args: iter([(item, b"{}")] ))
    calls: list[str] = []
    monkeypatch.setattr(
        "feedian.sync.fetch_page_text",
        lambda url, **_kwargs: calls.append(url) or PageFetchResult(url=url, final_url=url, text="article", media_type="text/html"),
    )
    config = VaultConfig(providers={"hatena": VaultConfig().providers["hatena"]})
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        sync_vault(store, config, source="hatena", fetch_comments=False)
        sync_vault(store, config, source="hatena", fetch_comments=False)
        sync_vault(store, config, source="hatena", fetch_comments=False, force_fetch=True)

        assert calls == [item.url, item.url]
    finally:
        store.close()


def test_sync_reports_each_processed_item(monkeypatch, tmp_path) -> None:
    items = [
        CanonicalItem(source="hatena", source_id=str(index), content_key=f"url:{index}", url=f"https://example.test/{index}")
        for index in range(2)
    ]
    monkeypatch.setattr("feedian.sync._provider_items", lambda *_args: iter((item, b"{}") for item in items))
    seen: list[tuple[int, str]] = []
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        sync_vault(
            store,
            VaultConfig(providers={"hatena": VaultConfig().providers["hatena"]}),
            source="hatena",
            fetch_pages=False,
            progress=lambda count, item: seen.append((count, item.source_id)),
        )
        assert seen == [(1, "0"), (2, "1")]
    finally:
        store.close()
