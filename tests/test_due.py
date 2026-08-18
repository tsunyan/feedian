from __future__ import annotations

from feedian.cli import _due_providers, _snapshot_is_due
from feedian.store import VaultStore
from feedian.vault import VaultConfig


def test_due_providers_are_tracked_independently(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        config = VaultConfig()
        config.providers["rss"].enabled = True
        config.providers["rss"].feeds = ["https://example.test/feed.xml"]
        run_id = store.create_sync_run(["rss"], "test")
        store.finish_sync_run(run_id, status="completed")

        assert _due_providers(store, config) == ["raindrop", "hatena"]
        assert _snapshot_is_due(store) is True
    finally:
        store.close()


def test_due_providers_ignores_a_completed_quick_run(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        config = VaultConfig()
        run_id = store.create_sync_run(["raindrop"], "test", mode="quick")
        store.finish_sync_run(run_id, status="completed")

        # A quick run never satisfies "due": it does not detect provider-side
        # edits or comment changes, so it must not push the next full sync out.
        assert _due_providers(store, config) == ["raindrop", "hatena"]
    finally:
        store.close()
