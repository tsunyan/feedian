from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from feedian.canonical import CanonicalItem
from feedian.extract import PageFetchResult
from feedian.hatena import HatenaEntryDiscussion, HatenaPublicComment
from feedian.rss import RssItem
from feedian.store import VaultStore
from feedian.sync import _provider_items, _select_hatena_comments, sync_vault
from feedian.vault import ProviderSettings, RssFeedSettings, VaultConfig


def test_sync_stores_provider_page_and_hatena_comments(monkeypatch, tmp_path) -> None:
    item = CanonicalItem(source="hatena", source_id="hatena-1", content_key="url:one", url="https://example.test/a", title="A")
    monkeypatch.setattr(
        "feedian.sync._provider_items",
        lambda config, provider, limit, **_kwargs: iter([(item, b'{"bookmark": 1}')]),
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
            entry_id="123",
            comments=[HatenaPublicComment(user="alice", comment="useful", tags=["tech"], timestamp="2026-08-11")],
        ),
    )
    monkeypatch.setattr("feedian.sync.fetch_hatena_bookmark_counts", lambda urls, **_kwargs: dict.fromkeys(urls, 1))
    monkeypatch.setattr("feedian.sync.fetch_hatena_star_counts", lambda urls, **_kwargs: dict.fromkeys(urls, 7))
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
        assert counts["payload"] == 1
        assert store.connection.execute(
            "SELECT COUNT(*) FROM payload WHERE lower(media_type) LIKE '%html%'"
        ).fetchone()[0] == 0
        assert store.connection.execute(
            "SELECT content_markdown FROM resource_revision"
        ).fetchone()[0] == "article body"
        comment = store.connection.execute(
            "SELECT star_count, posted_at, metadata_json FROM comment_revision"
        ).fetchone()
        assert tuple(comment) == (7, "2026-08-11", "{}")
        state = store.connection.execute(
            "SELECT entry_url, entry_id FROM resource_comment_state"
        ).fetchone()
        assert tuple(state) == (item.url, "123")
        assert store.latest_sync_run()["status"] == "completed"
    finally:
        store.close()


def test_hatena_comment_selection_keeps_twenty_by_stars_then_oldest(monkeypatch) -> None:
    comments = [
        HatenaPublicComment(
            user=f"user-{index:02d}",
            comment=f"comment {index}",
            timestamp=f"2026/08/{index + 1:02d} 00:00",
        )
        for index in range(25)
    ]
    discussion = HatenaEntryDiscussion(entry_id="123", comments=comments)

    def star_counts(urls, **_kwargs):
        return {
            url: (10 if "user-24" in url else 1)
            for url in urls
        }

    monkeypatch.setattr("feedian.sync.fetch_hatena_star_counts", star_counts)

    selected = _select_hatena_comments(discussion)

    assert len(selected) == 20
    assert selected[0].user == "user-24"
    assert [comment.user for comment in selected[1:]] == [f"user-{index:02d}" for index in range(19)]


def test_sync_marks_a_previous_running_process_as_interrupted(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("feedian.sync._provider_items", lambda *_args, **_kwargs: iter(()))
    config = VaultConfig(providers={"hatena": VaultConfig().providers["hatena"]})
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        interrupted_id = store.create_sync_run(["hatena"], "old")

        report = sync_vault(store, config, source="hatena", fetch_comments=False)

        interrupted = store.connection.execute(
            "SELECT status, error, finished_at FROM sync_run WHERE sync_run_id = ?",
            (interrupted_id,),
        ).fetchone()
        assert interrupted["status"] == "failed"
        assert interrupted["error"] == "interrupted before the next sync"
        assert interrupted["finished_at"]
        assert report.run_id != interrupted_id
        assert store.latest_sync_run()["status"] == "completed"
    finally:
        store.close()


def test_sync_skips_recent_page_fetches(monkeypatch, tmp_path) -> None:
    item = CanonicalItem(source="hatena", source_id="hatena-1", content_key="url:one", url="https://example.test/a", title="A")
    monkeypatch.setattr("feedian.sync._provider_items", lambda *_args, **_kwargs: iter([(item, b"{}")] ))
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


def test_sync_uses_page_validators_and_keeps_content_after_not_modified(monkeypatch, tmp_path) -> None:
    item = CanonicalItem(source="hatena", source_id="hatena-1", content_key="url:one", url="https://example.test/a", title="A")
    monkeypatch.setattr("feedian.sync._provider_items", lambda *_args, **_kwargs: iter([(item, b"{}")] ))
    calls: list[dict[str, object]] = []

    def fetch(url: str, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return PageFetchResult(
                url=url, final_url=url, text="article", media_type="text/html", response_headers={"ETag": '"one"'}
            )
        return PageFetchResult(
            url=url, final_url=url, text="", fetch_method="http", http_status=304, not_modified=True
        )

    monkeypatch.setattr("feedian.sync.fetch_page_text", fetch)
    config = VaultConfig(providers={"hatena": VaultConfig().providers["hatena"]})
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        sync_vault(store, config, source="hatena", fetch_comments=False)
        report = sync_vault(store, config, source="hatena", fetch_comments=False, force_fetch=True)

        assert calls[1]["etag"] == '"one"'
        assert calls[1]["last_modified"] == ""
        assert report.fetched == 1
        assert store.connection.execute("SELECT content_markdown FROM resource_revision").fetchone()[0] == "article"
        assert store.connection.execute("SELECT COUNT(*) FROM resource_revision").fetchone()[0] == 1
    finally:
        store.close()


def test_sync_reports_each_processed_item(monkeypatch, tmp_path) -> None:
    items = [
        CanonicalItem(source="hatena", source_id=str(index), content_key=f"url:{index}", url=f"https://example.test/{index}")
        for index in range(2)
    ]
    monkeypatch.setattr(
        "feedian.sync._provider_items", lambda *_args, **_kwargs: iter((item, b"{}") for item in items)
    )
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


def test_hatena_collection_reports_a_separate_collection_phase(monkeypatch) -> None:
    item = CanonicalItem(
        source="hatena", source_id="one", content_key="url:one", url="https://example.test"
    )
    seen: list[tuple[str, int, int]] = []

    def fetch_bookmarks(*_args, **kwargs):
        kwargs["on_page"](100, 200)
        kwargs["on_page"](200, 200)
        return [item]

    monkeypatch.setattr("feedian.sync._required_env", lambda _name: "value")
    monkeypatch.setattr("feedian.sync.fetch_hatena_bookmarks", fetch_bookmarks)
    rows = list(
        _provider_items(
            VaultConfig(providers={"hatena": VaultConfig().providers["hatena"]}),
            "hatena",
            200,
            collection_progress=lambda provider, collected, total: seen.append(
                (provider, collected, total)
            ),
        )
    )

    assert len(rows) == 1
    assert seen == [("hatena", 0, 0), ("hatena", 100, 200), ("hatena", 200, 200)]


def test_sync_fetches_comments_concurrently_and_stores_results(monkeypatch, tmp_path) -> None:
    items = [
        CanonicalItem(source="hatena", source_id=str(index), content_key=f"url:{index}", url=f"https://example.test/{index}")
        for index in range(3)
    ]
    monkeypatch.setattr(
        "feedian.sync._provider_items", lambda *_args, **_kwargs: iter((item, b"{}") for item in items)
    )
    monkeypatch.setattr(
        "feedian.sync.fetch_hatena_entry_discussion",
        lambda url: HatenaEntryDiscussion(
            entry_url=url,
            entry_id=url.rsplit("/", 1)[-1],
            comments=[HatenaPublicComment(user="alice-" + url[-1], comment="voice", timestamp="2026/08/12 00:00")],
        ),
    )
    monkeypatch.setattr("feedian.sync.fetch_hatena_bookmark_counts", lambda urls, **_kwargs: dict.fromkeys(urls, 0))
    monkeypatch.setattr("feedian.sync.fetch_hatena_star_counts", lambda urls, **_kwargs: dict.fromkeys(urls, 0))
    comment_progress: list[tuple[int, int]] = []
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        report = sync_vault(
            store,
            VaultConfig(providers={"hatena": VaultConfig().providers["hatena"]}),
            source="hatena",
            fetch_pages=False,
            comment_progress=lambda processed, total: comment_progress.append((processed, total)),
        )
        assert report.failed == 0
        assert store.status_counts()["comment"] == 3
        assert comment_progress == [(0, 3), (1, 3), (2, 3), (3, 3)]
    finally:
        store.close()


def test_sync_refreshes_comments_only_when_count_changes_or_forced(monkeypatch, tmp_path) -> None:
    item = CanonicalItem(
        source="raindrop", source_id="one", content_key="url:one",
        url="https://example.test/article", title="Article",
    )
    monkeypatch.setattr(
        "feedian.sync._provider_items", lambda *_args, **_kwargs: iter([(item, b"{}")])
    )
    current_count = [1]
    monkeypatch.setattr(
        "feedian.sync.fetch_hatena_bookmark_counts",
        lambda urls, **_kwargs: dict.fromkeys(urls, current_count[0]),
    )
    discussions = [
        HatenaEntryDiscussion(
            bookmark_count=1,
            comments=[
                HatenaPublicComment(user="alice", comment="first"),
                HatenaPublicComment(user="bob", comment="keep me"),
            ],
        ),
        HatenaEntryDiscussion(
            bookmark_count=1,
            comments=[HatenaPublicComment(user="alice", comment="forced")],
        ),
        HatenaEntryDiscussion(
            bookmark_count=2,
            comments=[HatenaPublicComment(user="alice", comment="changed")],
        ),
    ]
    calls: list[str] = []

    def fetch_discussion(url: str) -> HatenaEntryDiscussion:
        calls.append(url)
        return discussions.pop(0)

    monkeypatch.setattr("feedian.sync.fetch_hatena_entry_discussion", fetch_discussion)
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    config = VaultConfig(providers={"raindrop": VaultConfig().providers["raindrop"]})
    try:
        sync_vault(store, config, source="raindrop", fetch_pages=False)
        sync_vault(store, config, source="raindrop", fetch_pages=False)
        sync_vault(store, config, source="raindrop", fetch_pages=False, force_comments=True)
        current_count[0] = 2
        sync_vault(store, config, source="raindrop", fetch_pages=False)

        assert calls == [item.url, item.url, item.url]
        rows = store.connection.execute(
            """
            SELECT c.author, cr.body FROM comment AS c
            JOIN comment_revision AS cr ON cr.comment_revision_id = c.current_revision_id
            ORDER BY c.author
            """
        ).fetchall()
        assert [(row["author"], row["body"]) for row in rows] == [("alice", "changed")]
    finally:
        store.close()


def test_rss_provider_continues_after_one_feed_fails_and_limits_newest(monkeypatch) -> None:
    settings = ProviderSettings(
        folder="RSS",
        layout="feed/year/month",
        feeds=[
            RssFeedSettings(url="https://bad.test/feed.xml"),
            RssFeedSettings(url="https://good.test/feed.xml"),
        ],
    )
    config = VaultConfig(providers={"rss": settings})

    def fetch(feed_url: str, **_kwargs):
        if "bad" in feed_url:
            raise RuntimeError("unavailable")
        return [
            RssItem(
                CanonicalItem(
                    source="rss", source_id="old", content_key="url:old",
                    url="https://good.test/old", created_at="2025-01-01T00:00:00Z",
                ),
                b"old",
            ),
            RssItem(
                CanonicalItem(
                    source="rss", source_id="new", content_key="url:new",
                    url="https://good.test/new", created_at="2026-08-13T00:00:00Z",
                ),
                b"new",
            ),
        ]

    monkeypatch.setattr("feedian.sync.fetch_rss_items", fetch)
    errors: list[tuple[str, str, str]] = []

    rows = list(
        _provider_items(
            config,
            "rss",
            1,
            provider_error=lambda provider, source, error: errors.append((provider, source, str(error))),
        )
    )

    assert [item.source_id for item, _payload in rows] == ["new"]
    assert errors == [("rss", "https://bad.test/feed.xml", "unavailable")]


def test_sync_uses_embedded_rss_content_without_page_fetch(monkeypatch, tmp_path) -> None:
    item = CanonicalItem(
        source="rss",
        source_id="rss-one",
        content_key="url:rss-one",
        url="https://example.test/article",
        title="RSS Article",
        embedded_content="Feed body",
    )
    monkeypatch.setattr(
        "feedian.sync._provider_items", lambda *_args, **_kwargs: iter([(item, b"{}")])
    )
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        report = sync_vault(
            store,
            VaultConfig(providers={"rss": ProviderSettings(folder="RSS")}),
            source="rss",
            fetch_pages=False,
            fetch_comments=False,
        )

        row = store.connection.execute(
            "SELECT title, content_markdown FROM resource_revision"
        ).fetchone()
        assert report.failed == 0
        assert tuple(row) == ("RSS Article", "Feed body")
    finally:
        store.close()


def test_sync_keeps_embedded_rss_content_when_page_fetch_has_no_text(monkeypatch, tmp_path) -> None:
    item = CanonicalItem(
        source="rss", source_id="rss-one", content_key="url:rss-one",
        url="https://example.test/article", title="RSS Article", embedded_content="Feed body",
    )
    monkeypatch.setattr(
        "feedian.sync._provider_items", lambda *_args, **_kwargs: iter([(item, b"{}")])
    )
    monkeypatch.setattr(
        "feedian.sync.fetch_page_text",
        lambda *_args, **_kwargs: PageFetchResult(
            url=item.url, text="", error="HTTP 503", fetch_method="http"
        ),
    )
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        report = sync_vault(
            store,
            VaultConfig(providers={"rss": ProviderSettings(folder="RSS")}),
            source="rss",
            fetch_comments=False,
        )

        row = store.connection.execute(
            "SELECT content_markdown FROM resource_revision"
        ).fetchone()
        capture = store.connection.execute(
            "SELECT warning, extracted_by FROM fetch_capture ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        assert report.failed == 0
        assert row["content_markdown"] == "Feed body"
        assert tuple(capture) == ("HTTP 503", "http:rss-feed-fallback")
    finally:
        store.close()


def test_quick_sync_reports_skips_and_avoids_hatena_bookmark_count_lookup(monkeypatch, tmp_path) -> None:
    items = [
        CanonicalItem(source="hatena", source_id=str(index), content_key=f"url:{index}", url=f"https://example.test/{index}")
        for index in range(3)
    ]
    monkeypatch.setattr(
        "feedian.sync._provider_items",
        lambda *_args, **_kwargs: iter((item, b"{}") for item in items),
    )
    monkeypatch.setattr(
        "feedian.sync.fetch_page_text",
        lambda url, **_kwargs: PageFetchResult(url=url, final_url=url, text="body", media_type="text/html"),
    )
    count_calls: list[list[str]] = []
    monkeypatch.setattr(
        "feedian.sync.fetch_hatena_bookmark_counts",
        lambda urls, **_kwargs: count_calls.append(list(urls)) or dict.fromkeys(urls, 1),
    )
    monkeypatch.setattr(
        "feedian.sync.fetch_hatena_entry_discussion",
        lambda url: HatenaEntryDiscussion(entry_url=url, bookmark_count=1, comments=[]),
    )
    config = VaultConfig(providers={"hatena": VaultConfig().providers["hatena"]})
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        full_report = sync_vault(store, config, source="hatena", quick=False)
        assert full_report.processed == 3
        calls_after_full = len(count_calls)

        quick_report = sync_vault(store, config, source="hatena", quick=True)

        assert quick_report.processed == 0
        assert quick_report.skipped == 3
        assert len(count_calls) == calls_after_full
    finally:
        store.close()


def test_quick_sync_processes_only_new_items_and_fetches_their_body(monkeypatch, tmp_path) -> None:
    known_items = [
        CanonicalItem(source="hatena", source_id=str(index), content_key=f"url:{index}", url=f"https://example.test/{index}")
        for index in range(2)
    ]
    new_item = CanonicalItem(source="hatena", source_id="new", content_key="url:new", url="https://example.test/new")
    items = list(known_items)
    monkeypatch.setattr(
        "feedian.sync._provider_items",
        lambda *_args, **_kwargs: iter((item, b"{}") for item in items),
    )
    fetched_urls: list[str] = []
    monkeypatch.setattr(
        "feedian.sync.fetch_page_text",
        lambda url, **_kwargs: fetched_urls.append(url)
        or PageFetchResult(url=url, final_url=url, text="new body", media_type="text/html"),
    )
    config = VaultConfig(providers={"hatena": VaultConfig().providers["hatena"]})
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        sync_vault(store, config, source="hatena", quick=False, fetch_comments=False)
        fetched_urls.clear()
        items.append(new_item)

        report = sync_vault(store, config, source="hatena", quick=True, fetch_comments=False)

        assert report.processed == 1
        assert report.skipped == 2
        assert fetched_urls == [new_item.url]
        body = store.connection.execute(
            """
            SELECT content_markdown FROM resource_revision AS rr
            JOIN resource AS r ON r.current_revision_id = rr.resource_revision_id
            JOIN resource_identifier AS ri ON ri.resource_id = r.resource_id
            WHERE ri.namespace = 'url' AND ri.value = ?
            """,
            (new_item.url,),
        ).fetchone()[0]
        assert body == "new body"
    finally:
        store.close()


def test_quick_sync_skips_known_item_even_when_provider_metadata_changed(monkeypatch, tmp_path) -> None:
    item = CanonicalItem(source="hatena", source_id="one", content_key="url:one", url="https://example.test/one", title="Original")
    changed_item = CanonicalItem(
        source="hatena", source_id="one", content_key="url:one", url="https://example.test/one", title="Changed title"
    )
    current_item = [item]
    monkeypatch.setattr(
        "feedian.sync._provider_items",
        lambda *_args, **_kwargs: iter([(current_item[0], b"{}")]),
    )
    monkeypatch.setattr(
        "feedian.sync.fetch_page_text",
        lambda url, **_kwargs: PageFetchResult(url=url, final_url=url, text="body", media_type="text/html"),
    )
    config = VaultConfig(providers={"hatena": VaultConfig().providers["hatena"]})
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        sync_vault(store, config, source="hatena", quick=False, fetch_comments=False)
        source_item_id = store.connection.execute(
            "SELECT source_item_id FROM source_item WHERE native_id = ?", ("one",)
        ).fetchone()[0]
        revisions_before = store.connection.execute(
            "SELECT COUNT(*) FROM source_item_revision WHERE source_item_id = ?", (source_item_id,)
        ).fetchone()[0]
        current_item[0] = changed_item

        report = sync_vault(store, config, source="hatena", quick=True, fetch_comments=False)

        revisions_after = store.connection.execute(
            "SELECT COUNT(*) FROM source_item_revision WHERE source_item_id = ?", (source_item_id,)
        ).fetchone()[0]
        metadata_json_after = store.connection.execute(
            "SELECT metadata_json FROM source_item_revision WHERE source_item_id = ?", (source_item_id,)
        ).fetchone()[0]
        assert report.skipped == 1
        assert report.processed == 0
        assert revisions_after == revisions_before
        assert "Original" in metadata_json_after
        assert "Changed title" not in metadata_json_after
    finally:
        store.close()


def test_quick_body_only_pass_fetches_a_resource_left_without_a_revision(monkeypatch, tmp_path) -> None:
    item = CanonicalItem(source="hatena", source_id="one", content_key="url:one", url="https://example.test/one")
    monkeypatch.setattr(
        "feedian.sync._provider_items",
        lambda *_args, **_kwargs: iter([(item, b"{}")]),
    )
    fetched_urls: list[str] = []
    monkeypatch.setattr(
        "feedian.sync.fetch_page_text",
        lambda url, **_kwargs: fetched_urls.append(url)
        or PageFetchResult(url=url, final_url=url, text="retried body", media_type="text/html"),
    )
    config = VaultConfig(providers={"hatena": VaultConfig().providers["hatena"]})
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        sync_vault(store, config, source="hatena", quick=True, fetch_pages=False, fetch_comments=False)
        resource_row = store.connection.execute(
            """
            SELECT r.resource_id, r.current_revision_id FROM resource AS r
            JOIN resource_identifier AS ri ON ri.resource_id = r.resource_id
            WHERE ri.namespace = 'url' AND ri.value = ?
            """,
            (item.url,),
        ).fetchone()
        assert resource_row["current_revision_id"] is None

        report = sync_vault(store, config, source="hatena", quick=True, fetch_comments=False)

        assert report.processed == 0
        assert report.skipped == 1
        assert report.retried == 1
        assert fetched_urls == [item.url]
        content = store.connection.execute(
            "SELECT content_markdown FROM resource_revision WHERE resource_id = ?", (resource_row["resource_id"],)
        ).fetchone()[0]
        assert content == "retried body"
    finally:
        store.close()


def test_quick_body_only_pass_replays_no_validators_for_a_resource_with_a_stale_etag(monkeypatch, tmp_path) -> None:
    item = CanonicalItem(source="hatena", source_id="one", content_key="url:one", url="https://example.test/one")
    monkeypatch.setattr(
        "feedian.sync._provider_items",
        lambda *_args, **_kwargs: iter([(item, b"{}")]),
    )
    monkeypatch.setattr(
        "feedian.sync.fetch_page_text",
        lambda url, **_kwargs: PageFetchResult(
            url=url, text="", error="HTTP 500", fetch_method="http", response_headers={"ETag": "stale-etag"}
        ),
    )
    config = VaultConfig(providers={"hatena": VaultConfig().providers["hatena"]})
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        sync_vault(store, config, source="hatena", quick=True, fetch_comments=False)
        resource_row = store.connection.execute(
            """
            SELECT r.resource_id, r.current_revision_id FROM resource AS r
            JOIN resource_identifier AS ri ON ri.resource_id = r.resource_id
            WHERE ri.namespace = 'url' AND ri.value = ?
            """,
            (item.url,),
        ).fetchone()
        assert resource_row["current_revision_id"] is None
        assert store.connection.execute(
            "SELECT response_etag FROM fetch_capture WHERE resource_id = ?", (resource_row["resource_id"],)
        ).fetchone()[0] == "stale-etag"
        # The failed capture is fresh, so should_fetch_resource's 30-minute
        # cooldown would otherwise skip the retry below.
        store.connection.execute(
            "UPDATE fetch_capture SET fetched_at = ? WHERE resource_id = ?",
            ("2000-01-01T00:00:00+00:00", resource_row["resource_id"]),
        )
        store.connection.commit()
        calls: list[dict] = []
        monkeypatch.setattr(
            "feedian.sync.fetch_page_text",
            lambda url, **kwargs: calls.append(kwargs)
            or PageFetchResult(url=url, final_url=url, text="fresh body", media_type="text/html"),
        )

        report = sync_vault(store, config, source="hatena", quick=True, fetch_comments=False)

        assert report.retried == 1
        assert len(calls) == 1
        assert calls[0]["etag"] == ""
        assert calls[0]["last_modified"] == ""
    finally:
        store.close()


def test_failed_page_fetch_records_fetch_capture_without_a_resource_revision(monkeypatch, tmp_path) -> None:
    item = CanonicalItem(source="hatena", source_id="one", content_key="url:one", url="https://example.test/one")
    monkeypatch.setattr(
        "feedian.sync._provider_items",
        lambda *_args, **_kwargs: iter([(item, b"{}")]),
    )
    monkeypatch.setattr(
        "feedian.sync.fetch_page_text",
        lambda *_args, **_kwargs: PageFetchResult(url=item.url, text="", error="HTTP 503", fetch_method="http"),
    )
    config = VaultConfig(providers={"hatena": VaultConfig().providers["hatena"]})
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        report = sync_vault(store, config, source="hatena", quick=False, fetch_comments=False)

        assert report.failed == 0
        assert store.connection.execute("SELECT COUNT(*) FROM resource_revision").fetchone()[0] == 0
        resource_row = store.connection.execute("SELECT current_revision_id FROM resource").fetchone()
        assert resource_row["current_revision_id"] is None
        capture = store.connection.execute(
            "SELECT warning FROM fetch_capture ORDER BY fetched_at DESC LIMIT 1"
        ).fetchone()
        assert capture["warning"] == "HTTP 503"
    finally:
        store.close()


def test_failed_page_fetch_resource_stays_in_unfetched_resources(monkeypatch, tmp_path) -> None:
    item = CanonicalItem(source="hatena", source_id="one", content_key="url:one", url="https://example.test/one")
    monkeypatch.setattr(
        "feedian.sync._provider_items",
        lambda *_args, **_kwargs: iter([(item, b"{}")]),
    )
    monkeypatch.setattr(
        "feedian.sync.fetch_page_text",
        lambda *_args, **_kwargs: PageFetchResult(url=item.url, text="", error="HTTP 503", fetch_method="http"),
    )
    config = VaultConfig(providers={"hatena": VaultConfig().providers["hatena"]})
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        sync_vault(store, config, source="hatena", quick=False, fetch_comments=False)

        pending = store.unfetched_resources(["hatena"])

        assert [url for _resource_id, url in pending] == [item.url]
    finally:
        store.close()


def test_quick_body_only_pass_records_sync_run_item_for_every_provider_sharing_a_resource(monkeypatch, tmp_path) -> None:
    raindrop_item = CanonicalItem(source="raindrop", source_id="rd-1", content_key="url:shared", url="https://example.test/shared")
    hatena_item = CanonicalItem(source="hatena", source_id="ht-1", content_key="url:shared", url="https://example.test/shared")
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        raindrop_stored = store.upsert_canonical_item(raindrop_item)
        hatena_stored = store.upsert_canonical_item(hatena_item)
        assert raindrop_stored.resource_id == hatena_stored.resource_id

        def fake_provider_items(config, provider, limit, **_kwargs):
            if provider == "raindrop":
                return iter([(raindrop_item, b"{}")])
            if provider == "hatena":
                return iter([(hatena_item, b"{}")])
            return iter(())

        monkeypatch.setattr("feedian.sync._provider_items", fake_provider_items)
        fetch_calls: list[str] = []
        monkeypatch.setattr(
            "feedian.sync.fetch_page_text",
            lambda url, **_kwargs: fetch_calls.append(url)
            or PageFetchResult(url=url, final_url=url, text="shared body", media_type="text/html"),
        )
        config = VaultConfig(
            providers={
                "raindrop": VaultConfig().providers["raindrop"],
                "hatena": VaultConfig().providers["hatena"],
            }
        )

        report = sync_vault(store, config, source="all", quick=True, fetch_comments=False)

        assert report.retried == 1
        assert fetch_calls == [raindrop_item.url]

        recorded_source_items = {
            str(row["source_item_id"])
            for row in store.connection.execute(
                "SELECT source_item_id FROM sync_run_item WHERE sync_run_id = ?", (report.run_id,)
            ).fetchall()
        }
        assert recorded_source_items == {raindrop_stored.source_item_id, hatena_stored.source_item_id}
    finally:
        store.close()


def test_quick_item_loop_body_fetch_records_outcome_for_every_source_item_sharing_a_resource(
    monkeypatch, tmp_path
) -> None:
    # The Hatena item is already known (registered by an earlier run) but its
    # resource never got a body. The Raindrop item is new and shares the same
    # URL/resource, so it is the one that reaches the item loop's own fetch.
    hatena_item = CanonicalItem(source="hatena", source_id="ht-1", content_key="url:shared", url="https://example.test/shared")
    raindrop_item = CanonicalItem(source="raindrop", source_id="rd-1", content_key="url:shared", url="https://example.test/shared")
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        hatena_stored = store.upsert_canonical_item(hatena_item)

        def fake_provider_items(config, provider, limit, **_kwargs):
            if provider == "raindrop":
                return iter([(raindrop_item, b"{}")])
            if provider == "hatena":
                return iter([(hatena_item, b"{}")])
            return iter(())

        monkeypatch.setattr("feedian.sync._provider_items", fake_provider_items)
        fetch_calls: list[str] = []
        monkeypatch.setattr(
            "feedian.sync.fetch_page_text",
            lambda url, **_kwargs: fetch_calls.append(url)
            or PageFetchResult(url=url, final_url=url, text="shared body", media_type="text/html"),
        )
        # Raindrop first: its item-loop pass fetches the shared resource's body
        # before Hatena's own body-only pass gets a chance to do it instead.
        config = VaultConfig(
            providers={
                "raindrop": VaultConfig().providers["raindrop"],
                "hatena": VaultConfig().providers["hatena"],
            }
        )

        report = sync_vault(store, config, source="all", quick=True, fetch_comments=False)

        assert fetch_calls == [raindrop_item.url]

        recorded = {
            (row["provider"], row["native_id"])
            for row in store.connection.execute(
                """
                SELECT si.provider AS provider, si.native_id AS native_id
                FROM sync_run_item AS sri
                JOIN source_item AS si ON si.source_item_id = sri.source_item_id
                WHERE sri.sync_run_id = ?
                """,
                (report.run_id,),
            ).fetchall()
        }
        assert recorded == {("hatena", "ht-1"), ("raindrop", "rd-1")}
        assert hatena_stored.resource_id
    finally:
        store.close()


def test_quick_limit_is_a_shared_budget_across_item_loop_and_body_only_pass(monkeypatch, tmp_path) -> None:
    pending_item = CanonicalItem(source="hatena", source_id="pending", content_key="url:pending", url="https://example.test/pending")
    new_item = CanonicalItem(source="hatena", source_id="new", content_key="url:new", url="https://example.test/new")
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        store.upsert_canonical_item(pending_item)
        monkeypatch.setattr(
            "feedian.sync._provider_items",
            lambda *_args, **_kwargs: iter([(new_item, b"{}")]),
        )
        monkeypatch.setattr(
            "feedian.sync.fetch_page_text",
            lambda url, **_kwargs: PageFetchResult(url=url, final_url=url, text="body", media_type="text/html"),
        )
        config = VaultConfig(providers={"hatena": VaultConfig().providers["hatena"]})

        report = sync_vault(store, config, source="hatena", quick=True, limit=1, fetch_comments=False)

        assert report.processed == 1
        assert report.retried == 0
    finally:
        store.close()


def test_quick_limit_budget_is_fetch_attempts_not_items_processed(monkeypatch, tmp_path) -> None:
    # The new item's resource already holds a body (as if fetched by an earlier
    # run), so the item loop processes it but makes no HTTP request for it. The
    # limit=1 budget must still be available to the body-only pass, which has a
    # bodyless resource of its own waiting.
    existing_item = CanonicalItem(
        source="hatena", source_id="existing", content_key="url:shared", url="https://example.test/shared"
    )
    new_item = CanonicalItem(source="hatena", source_id="new", content_key="url:shared", url="https://example.test/shared")
    pending_item = CanonicalItem(source="hatena", source_id="pending", content_key="url:pending", url="https://example.test/pending")
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        existing_stored = store.upsert_canonical_item(existing_item)
        store.record_resource_revision(existing_stored.resource_id or "", content_markdown="already fetched")
        store.upsert_canonical_item(pending_item)
        monkeypatch.setattr(
            "feedian.sync._provider_items",
            lambda *_args, **_kwargs: iter([(new_item, b"{}")]),
        )
        fetch_calls: list[str] = []
        monkeypatch.setattr(
            "feedian.sync.fetch_page_text",
            lambda url, **_kwargs: fetch_calls.append(url)
            or PageFetchResult(url=url, final_url=url, text="body", media_type="text/html"),
        )
        config = VaultConfig(providers={"hatena": VaultConfig().providers["hatena"]})

        report = sync_vault(store, config, source="hatena", quick=True, limit=1, fetch_comments=False)

        assert report.processed == 1
        assert fetch_calls == [pending_item.url]
        assert report.retried == 1
    finally:
        store.close()


def test_quick_without_fetch_pages_skips_body_only_pass_even_with_candidates_pending(monkeypatch, tmp_path) -> None:
    pending_item = CanonicalItem(source="hatena", source_id="pending", content_key="url:pending", url="https://example.test/pending")
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        store.upsert_canonical_item(pending_item)
        monkeypatch.setattr("feedian.sync._provider_items", lambda *_args, **_kwargs: iter(()))
        config = VaultConfig(providers={"hatena": VaultConfig().providers["hatena"]})

        report = sync_vault(store, config, source="hatena", quick=True, fetch_pages=False, fetch_comments=False)

        assert report.retried == 0
        assert report.fetched == 0
    finally:
        store.close()


def _raindrop_raw_items(ids: list[str]) -> list[dict]:
    return [{"_id": item_id, "link": f"https://example.test/{item_id}"} for item_id in ids]


def test_raindrop_quick_collection_stops_after_a_fully_known_page(monkeypatch) -> None:
    known_ids = [f"r{index}" for index in range(50)]
    page = _raindrop_raw_items(known_ids)

    class _StubClient:
        def __init__(self, token: str) -> None:
            self.token = token

        def iter_raindrop_pages(self, collection_id: int, per_page: int, nested: bool):
            yield page

    monkeypatch.setattr("feedian.sync._required_env", lambda _name: "token")
    monkeypatch.setattr("feedian.sync.RaindropClient", _StubClient)
    stopped: list[str] = []
    config = VaultConfig(providers={"raindrop": VaultConfig().providers["raindrop"]})

    rows = list(
        _provider_items(
            config, "raindrop", None, quick=True, known=set(known_ids),
            stop_after_known_pages=1, on_stopped_early=stopped.append,
        )
    )

    assert len(rows) == 50
    assert stopped == ["raindrop"]


def test_raindrop_quick_collection_continues_past_a_mixed_known_and_new_page(monkeypatch) -> None:
    known_ids = [f"r{index}" for index in range(25)]
    new_ids = [f"n{index}" for index in range(25)]
    page = _raindrop_raw_items(known_ids + new_ids)

    class _StubClient:
        def __init__(self, token: str) -> None:
            self.token = token

        def iter_raindrop_pages(self, collection_id: int, per_page: int, nested: bool):
            yield page

    monkeypatch.setattr("feedian.sync._required_env", lambda _name: "token")
    monkeypatch.setattr("feedian.sync.RaindropClient", _StubClient)
    stopped: list[str] = []
    config = VaultConfig(providers={"raindrop": VaultConfig().providers["raindrop"]})

    rows = list(
        _provider_items(
            config, "raindrop", None, quick=True, known=set(known_ids),
            stop_after_known_pages=1, on_stopped_early=stopped.append,
        )
    )

    assert len(rows) == 50
    assert stopped == []


def test_a_raising_should_fetch_resource_fails_the_item_not_the_run(monkeypatch, tmp_path) -> None:
    item = CanonicalItem(source="hatena", source_id="one", content_key="url:one", url="https://example.test/one")
    monkeypatch.setattr(
        "feedian.sync._provider_items",
        lambda *_args, **_kwargs: iter([(item, b"{}")]),
    )
    config = VaultConfig(providers={"hatena": VaultConfig().providers["hatena"]})
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        def explode(*_args, **_kwargs):
            raise RuntimeError("corrupt fetched_at")

        monkeypatch.setattr(store, "should_fetch_resource", explode)

        report = sync_vault(store, config, source="hatena", fetch_comments=False)

        assert report.failed == 1
        assert store.latest_sync_run()["status"] == "partial"
        row = store.connection.execute("SELECT status, error FROM sync_run_item").fetchone()
        assert row["status"] == "failed"
        assert "corrupt fetched_at" in row["error"]
    finally:
        store.close()


def test_quick_body_only_pass_records_a_capture_when_the_fetch_raises(monkeypatch, tmp_path) -> None:
    item = CanonicalItem(source="hatena", source_id="one", content_key="url:one", url="https://example.test/one")
    monkeypatch.setattr(
        "feedian.sync._provider_items",
        lambda *_args, **_kwargs: iter([(item, b"{}")]),
    )
    config = VaultConfig(providers={"hatena": VaultConfig().providers["hatena"]})
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        # Store the item without a body, so the next run sees it as a (B1) candidate.
        sync_vault(store, config, source="hatena", quick=True, fetch_pages=False, fetch_comments=False)
        resource_id = store.connection.execute("SELECT resource_id FROM resource").fetchone()[0]
        assert store.connection.execute(
            "SELECT COUNT(*) FROM fetch_capture WHERE resource_id = ?", (resource_id,)
        ).fetchone()[0] == 0

        def explode(*_args, **_kwargs):
            raise RuntimeError("decode blew up")

        monkeypatch.setattr("feedian.sync.fetch_page_text", explode)

        report = sync_vault(store, config, source="hatena", quick=True, fetch_comments=False)

        assert report.failed == 1
        capture = store.connection.execute(
            "SELECT warning, fetched_at FROM fetch_capture WHERE resource_id = ?", (resource_id,)
        ).fetchone()
        # Without the capture the resource keeps a NULL fetched_at and sorts first
        # for every later run, taking the budget from everything behind it.
        assert capture is not None
        assert "decode blew up" in capture["warning"]
        assert capture["fetched_at"]
        assert store.connection.execute(
            "SELECT current_revision_id FROM resource WHERE resource_id = ?", (resource_id,)
        ).fetchone()[0] is None
    finally:
        store.close()


def test_raindrop_quick_collection_stops_at_limit_new_items_even_without_a_fetch(monkeypatch) -> None:
    """`--limit` bounds items collected, not body fetches.

    A deliberate divergence from a literal reading of the specification's
    "count the budget by items actually fetched". That sentence governs the
    body-only pass, whose candidates may be gated out by should_fetch_resource.
    Applying it to collection would unbound `--limit` whenever nothing needs
    fetching: a second provider bookmarking URLs another provider already
    stored yields new source items whose resources all hold bodies, so no
    fetch ever occurs and the whole collection would be paged and upserted.
    See docs/reviews/20260818-sync-quick-mode-implementation.ja.md.
    """
    page = _raindrop_raw_items([f"n{index}" for index in range(50)])

    class _StubClient:
        def __init__(self, token: str) -> None:
            self.token = token

        def iter_raindrop_pages(self, collection_id: int, per_page: int, nested: bool):
            yield page

    monkeypatch.setattr("feedian.sync._required_env", lambda _name: "token")
    monkeypatch.setattr("feedian.sync.RaindropClient", _StubClient)
    stopped: list[str] = []
    config = VaultConfig(providers={"raindrop": VaultConfig().providers["raindrop"]})

    rows = list(
        _provider_items(
            config, "raindrop", 1, quick=True, known=set(),
            stop_after_known_pages=1, on_stopped_early=stopped.append,
        )
    )

    assert len(rows) == 1
    assert stopped == []


def test_raindrop_quick_collection_ends_on_a_short_page_without_stopping_early(monkeypatch) -> None:
    known_ids = [f"r{index}" for index in range(10)]
    page = _raindrop_raw_items(known_ids)

    class _StubClient:
        def __init__(self, token: str) -> None:
            self.token = token

        def iter_raindrop_pages(self, collection_id: int, per_page: int, nested: bool):
            yield page

    monkeypatch.setattr("feedian.sync._required_env", lambda _name: "token")
    monkeypatch.setattr("feedian.sync.RaindropClient", _StubClient)
    stopped: list[str] = []
    config = VaultConfig(providers={"raindrop": VaultConfig().providers["raindrop"]})

    rows = list(
        _provider_items(
            config, "raindrop", None, quick=True, known=set(known_ids),
            stop_after_known_pages=1, on_stopped_early=stopped.append,
        )
    )

    assert len(rows) == 10
    assert stopped == []


def test_settings_fingerprint_differs_between_quick_and_full_runs(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("feedian.sync._provider_items", lambda *_args, **_kwargs: iter(()))
    config = VaultConfig(providers={"hatena": VaultConfig().providers["hatena"]})
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        full_report = sync_vault(store, config, source="hatena", quick=False, fetch_comments=False)
        quick_report = sync_vault(store, config, source="hatena", quick=True, fetch_comments=False)

        fingerprints = {
            row["sync_run_id"]: row["settings_fingerprint"]
            for row in store.connection.execute("SELECT sync_run_id, settings_fingerprint FROM sync_run").fetchall()
        }
        assert fingerprints[full_report.run_id] != fingerprints[quick_report.run_id]
    finally:
        store.close()


@pytest.mark.parametrize("value", [1.5, True, "2", 0, -1])
def test_quick_rejects_a_non_integer_or_out_of_range_stop_after_known_pages(monkeypatch, tmp_path, value) -> None:
    monkeypatch.setattr("feedian.sync._provider_items", lambda *_args, **_kwargs: iter(()))
    config = VaultConfig(providers={"hatena": VaultConfig().providers["hatena"]})
    config.fetch["quick_stop_after_known_pages"] = value
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        with pytest.raises(ValueError):
            sync_vault(store, config, source="hatena", quick=True, fetch_comments=False)
    finally:
        store.close()


def test_quick_accepts_an_integer_stop_after_known_pages(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("feedian.sync._provider_items", lambda *_args, **_kwargs: iter(()))
    config = VaultConfig(providers={"hatena": VaultConfig().providers["hatena"]})
    config.fetch["quick_stop_after_known_pages"] = 2
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        report = sync_vault(store, config, source="hatena", quick=True, fetch_comments=False)

        assert report.processed == 0
    finally:
        store.close()


def test_full_sync_suppresses_a_resource_with_a_terminal_capture(monkeypatch, tmp_path) -> None:
    item = CanonicalItem(source="hatena", source_id="one", content_key="url:one", url="https://example.test/one", title="A")
    monkeypatch.setattr("feedian.sync._provider_items", lambda *_args, **_kwargs: iter([(item, b"{}")]))
    calls: list[str] = []
    monkeypatch.setattr(
        "feedian.sync.fetch_page_text",
        lambda url, **_kwargs: calls.append(url)
        or PageFetchResult(url=url, final_url=url, text="body", media_type="text/html"),
    )
    config = VaultConfig(providers={"hatena": VaultConfig().providers["hatena"]})
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        stored = store.upsert_canonical_item(item)
        store.record_failed_fetch(stored.resource_id or "", warning="HTTP 404", http_status=404)

        report = sync_vault(store, config, source="hatena", quick=False, fetch_comments=False)

        assert calls == []
        assert report.fetched == 0
    finally:
        store.close()


def test_sync_stores_a_terminal_http_status_on_the_capture(monkeypatch, tmp_path) -> None:
    item = CanonicalItem(source="hatena", source_id="one", content_key="url:one", url="https://example.test/one", title="A")
    monkeypatch.setattr("feedian.sync._provider_items", lambda *_args, **_kwargs: iter([(item, b"{}")]))
    monkeypatch.setattr(
        "feedian.sync.fetch_page_text",
        lambda *_args, **_kwargs: PageFetchResult(
            url=item.url, text="", error="HTTP 404", fetch_method="http", http_status=404
        ),
    )
    config = VaultConfig(providers={"hatena": VaultConfig().providers["hatena"]})
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        sync_vault(store, config, source="hatena", quick=False, fetch_comments=False)

        http_status = store.connection.execute(
            "SELECT http_status FROM fetch_capture ORDER BY fetched_at DESC LIMIT 1"
        ).fetchone()["http_status"]
        assert http_status == 404
    finally:
        store.close()


def test_quick_body_only_pass_does_not_charge_the_limit_budget_for_a_suppressed_resource(monkeypatch, tmp_path) -> None:
    suppressed_item = CanonicalItem(
        source="hatena", source_id="suppressed", content_key="url:suppressed", url="https://example.test/suppressed",
    )
    fetchable_item = CanonicalItem(
        source="hatena", source_id="fetchable", content_key="url:fetchable", url="https://example.test/fetchable",
    )
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        # unfetched_resources orders never-fetched resources (NULL fetched_at)
        # first, so both resources here carry a real, aged capture -- the
        # suppressed one older -- to make sure the suppressed one is evaluated
        # (and skipped without spending the budget) before the fetchable one.
        suppressed_stored = store.upsert_canonical_item(suppressed_item)
        store.record_failed_fetch(suppressed_stored.resource_id or "", warning="HTTP 404", http_status=404)
        store.connection.execute(
            "UPDATE fetch_capture SET fetched_at = ? WHERE resource_id = ?",
            ((datetime.now(timezone.utc) - timedelta(days=400)).isoformat(), suppressed_stored.resource_id),
        )
        store.connection.commit()
        fetchable_stored = store.upsert_canonical_item(fetchable_item)
        store.record_failed_fetch(fetchable_stored.resource_id or "", warning="HTTP 500")
        store.connection.execute(
            "UPDATE fetch_capture SET fetched_at = ? WHERE resource_id = ?",
            ((datetime.now(timezone.utc) - timedelta(minutes=40)).isoformat(), fetchable_stored.resource_id),
        )
        store.connection.commit()

        monkeypatch.setattr("feedian.sync._provider_items", lambda *_args, **_kwargs: iter(()))
        fetch_calls: list[str] = []
        monkeypatch.setattr(
            "feedian.sync.fetch_page_text",
            lambda url, **_kwargs: fetch_calls.append(url)
            or PageFetchResult(url=url, final_url=url, text="body", media_type="text/html"),
        )
        config = VaultConfig(providers={"hatena": VaultConfig().providers["hatena"]})

        report = sync_vault(store, config, source="hatena", quick=True, limit=1, fetch_comments=False)

        assert fetch_calls == [fetchable_item.url]
        assert report.retried == 1
    finally:
        store.close()


def test_sync_passes_the_configured_timeout_seconds_to_fetch_page_text(monkeypatch, tmp_path) -> None:
    item = CanonicalItem(source="hatena", source_id="one", content_key="url:one", url="https://example.test/one", title="A")
    monkeypatch.setattr("feedian.sync._provider_items", lambda *_args, **_kwargs: iter([(item, b"{}")]))
    captured_kwargs: dict[str, object] = {}
    monkeypatch.setattr(
        "feedian.sync.fetch_page_text",
        lambda url, **kwargs: captured_kwargs.update(kwargs)
        or PageFetchResult(url=url, final_url=url, text="body", media_type="text/html"),
    )
    config = VaultConfig(providers={"hatena": VaultConfig().providers["hatena"]})
    config.fetch["timeout_seconds"] = 8
    config.fetch["browser_timeout_seconds"] = 45
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        sync_vault(store, config, source="hatena", quick=False, fetch_comments=False)

        assert captured_kwargs["timeout_seconds"] == 8
        assert captured_kwargs["timeout_seconds"] != 30
        assert captured_kwargs["browser_timeout_seconds"] == 45
    finally:
        store.close()


def test_quick_body_only_pass_also_passes_the_configured_timeout_seconds(monkeypatch, tmp_path) -> None:
    item = CanonicalItem(
        source="hatena", source_id="one", content_key="url:one", url="https://example.test/one", title="A",
    )
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        store.upsert_canonical_item(item)
        monkeypatch.setattr("feedian.sync._provider_items", lambda *_args, **_kwargs: iter(()))
        captured_kwargs: dict[str, object] = {}
        monkeypatch.setattr(
            "feedian.sync.fetch_page_text",
            lambda url, **kwargs: captured_kwargs.update(kwargs)
            or PageFetchResult(url=url, final_url=url, text="body", media_type="text/html"),
        )
        config = VaultConfig(providers={"hatena": VaultConfig().providers["hatena"]})
        config.fetch["timeout_seconds"] = 9
        config.fetch["browser_timeout_seconds"] = 41

        sync_vault(store, config, source="hatena", quick=True, fetch_comments=False)

        assert captured_kwargs["timeout_seconds"] == 9
        assert captured_kwargs["browser_timeout_seconds"] == 41
    finally:
        store.close()


def test_sync_records_the_dns_failure_kind_on_the_capture(monkeypatch, tmp_path) -> None:
    """Wiring test for `_store_page`: without `failure_kind=page.failure_kind`,
    `extract.py`'s classification never reaches the database and the terminal
    mechanism has nothing to act on."""
    item = CanonicalItem(source="hatena", source_id="one", content_key="url:one", url="https://example.test/one", title="A")
    monkeypatch.setattr("feedian.sync._provider_items", lambda *_args, **_kwargs: iter([(item, b"{}")]))
    monkeypatch.setattr(
        "feedian.sync.fetch_page_text",
        lambda *_args, **_kwargs: PageFetchResult(
            url=item.url, text="", error="blocked URL: hostname could not be resolved", failure_kind="dns"
        ),
    )
    config = VaultConfig(providers={"hatena": VaultConfig().providers["hatena"]})
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        sync_vault(store, config, source="hatena", quick=False, fetch_comments=False)

        failure_kind = store.connection.execute(
            "SELECT failure_kind FROM fetch_capture ORDER BY fetched_at DESC LIMIT 1"
        ).fetchone()["failure_kind"]
        assert failure_kind == "dns"
    finally:
        store.close()


def test_full_sync_suppresses_a_resource_using_configured_terminal_kind_settings(monkeypatch, tmp_path) -> None:
    """Uses non-default `terminal_failure_kinds`/`terminal_kind_failures` so a
    match against the store's own defaults cannot pass this test by accident."""
    item = CanonicalItem(source="hatena", source_id="one", content_key="url:one", url="https://example.test/one", title="A")
    monkeypatch.setattr("feedian.sync._provider_items", lambda *_args, **_kwargs: iter([(item, b"{}")]))
    calls: list[str] = []
    monkeypatch.setattr(
        "feedian.sync.fetch_page_text",
        lambda url, **_kwargs: calls.append(url)
        or PageFetchResult(url=url, final_url=url, text="body", media_type="text/html"),
    )
    config = VaultConfig(providers={"hatena": VaultConfig().providers["hatena"]})
    config.fetch["terminal_failure_kinds"] = ["timeout"]
    config.fetch["terminal_kind_failures"] = 1
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        stored = store.upsert_canonical_item(item)
        # A single timeout failure. The default terminal_kind_failures (3)
        # would not suppress this; the configured value (1) does.
        store.record_failed_fetch(stored.resource_id or "", warning="timed out", failure_kind="timeout")

        report = sync_vault(store, config, source="hatena", quick=False, fetch_comments=False)

        assert calls == []
        assert report.fetched == 0
    finally:
        store.close()
