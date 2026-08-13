from __future__ import annotations

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
