from __future__ import annotations

import json

from feedian.canonical import CanonicalItem
from feedian.hatena import fetch_hatena_star_counts
from feedian.stars import enrich_hatena_stars
from feedian.store import VaultStore


def test_star_enrichment_updates_comment_revision(monkeypatch, tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(
            CanonicalItem(source="hatena", source_id="one", content_key="url:one", url="https://example.test")
        )
        star_url = "https://b.hatena.ne.jp/alice/20260812#bookmark-123"
        store.upsert_comment(
            provider="hatena", resource_id=item.resource_id or "", author="alice", body="comment",
            metadata={"star_url": star_url},
        )
        monkeypatch.setattr("feedian.stars.fetch_hatena_star_counts", lambda _uris: {star_url: 7})

        report = enrich_hatena_stars(store)

        assert report.updated == 1
        row = store.connection.execute("SELECT star_count FROM comment_revision ORDER BY created_at DESC LIMIT 1").fetchone()
        assert row[0] == 7
    finally:
        store.close()


def test_star_api_omission_means_zero_stars(monkeypatch) -> None:
    monkeypatch.setattr("feedian.hatena._read_entry_json", lambda *_args: {"entries": []})

    assert fetch_hatena_star_counts(["https://b.hatena.ne.jp/alice/20260812#bookmark-123"]) == {
        "https://b.hatena.ne.jp/alice/20260812#bookmark-123": 0
    }
