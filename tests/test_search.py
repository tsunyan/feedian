from __future__ import annotations

import sqlite3

from feedian.canonical import CanonicalItem
from feedian.search import rebuild_search_index, search_index_generation
from feedian.store import VaultStore


def test_search_index_is_separate_rebuildable_and_generation_tracked(tmp_path) -> None:
    main_path = tmp_path / "feedian.sqlite3"
    search_path = tmp_path / "cache" / "search.sqlite3"
    store = VaultStore.open(main_path)
    try:
        item = store.upsert_canonical_item(
            CanonicalItem(
                source="hatena", source_id="one", content_key="url:one",
                url="https://example.test", title="Search title", comment="bookmark voice",
            )
        )
        store.record_resource_revision(item.resource_id or "", content_markdown="searchable article")
        store.upsert_comment(
            provider="hatena", resource_id=item.resource_id or "", author="alice", body="searchable comment"
        )

        first = rebuild_search_index(store, search_path)
        second = rebuild_search_index(store, search_path)

        assert first.rebuilt is True
        assert second.rebuilt is False
        assert search_index_generation(search_path) == store.search_generation()
        assert not store.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name IN ('source_fts', 'resource_fts', 'comment_fts')"
        ).fetchone()
        search = sqlite3.connect(search_path)
        try:
            assert search.execute(
                "SELECT COUNT(*) FROM resource_fts WHERE resource_fts MATCH 'searchable'"
            ).fetchone()[0] == 1
            assert search.execute(
                "SELECT COUNT(*) FROM comment_fts WHERE comment_fts MATCH 'searchable'"
            ).fetchone()[0] == 1
        finally:
            search.close()
    finally:
        store.close()
