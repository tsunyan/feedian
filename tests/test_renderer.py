from __future__ import annotations

from feedian.canonical import CanonicalItem
from feedian.renderer import render_raw_views
from feedian.store import VaultStore
from feedian.vault import VaultConfig, initialize_vault


def test_renderer_stages_raw_and_comments_without_touching_raw(tmp_path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    initialize_vault(root)
    config = VaultConfig(providers={"hatena": VaultConfig().providers["hatena"]})
    store = VaultStore.open(root / ".feedian" / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(
            CanonicalItem(source="hatena", source_id="hatena-1", content_key="url:one", url="https://example.test/a", title="Article")
        )
        revision, _ = store.record_resource_revision(item.resource_id or "", content_markdown="Body", discussion_text="Reply")
        store.put_asset(
            resource_id=item.resource_id or "",
            resource_revision_id=revision,
            content=b"\x89PNG\r\n\x1a\nimage",
            media_type="image/png",
            source_url="https://example.test/image.png",
            alt_text="Example image",
        )
        store.upsert_comment(provider="hatena", resource_id=item.resource_id or "", author="alice", body="Useful", star_count=3)

        report = render_raw_views(store, root, config)

        staged = root / ".feedian" / "staging" / "raw" / "Hatena" / "Article - hatena-1.md"
        comments = root / ".feedian" / "staging" / "raw" / "Hatena" / "Article - hatena-1.comments.md"
        assert report.written == 1
        assert report.comments_written == 1
        assert staged.exists()
        assert comments.exists()
        assert not (root / "raw").exists()
        assert "## Content (Original)" in staged.read_text(encoding="utf-8")
        assert "feedian_managed: true" in staged.read_text(encoding="utf-8")
        assert "tags: []" in staged.read_text(encoding="utf-8")
        assert "![Example image](../assets/" in staged.read_text(encoding="utf-8")
        assert list((root / ".feedian" / "staging" / "raw" / "assets").glob("*.png"))
        assert "★3" in comments.read_text(encoding="utf-8")
    finally:
        store.close()


def test_apply_does_not_overwrite_legacy_or_edited_raw_note(tmp_path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    initialize_vault(root)
    config = VaultConfig(providers={"hatena": VaultConfig().providers["hatena"]})
    legacy = root / "raw" / "Hatena" / "Article - hatena-1.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("human content\n", encoding="utf-8")
    store = VaultStore.open(root / ".feedian" / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(
            CanonicalItem(source="hatena", source_id="hatena-1", content_key="url:one", url="https://example.test/a", title="Article")
        )
        store.record_resource_revision(item.resource_id or "", content_markdown="Body")

        report = render_raw_views(store, root, config, apply=True)

        assert report.conflicts == 1
        assert legacy.read_text(encoding="utf-8") == "human content\n"
    finally:
        store.close()


def test_apply_replaces_only_an_exactly_archived_legacy_note(tmp_path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    initialize_vault(root)
    config = VaultConfig(providers={"hatena": VaultConfig().providers["hatena"]})
    legacy = root / "raw" / "Hatena" / "Article - hatena-1.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"legacy\r\n")
    store = VaultStore.open(root / ".feedian" / "feedian.sqlite3")
    try:
        store.import_legacy_artifacts(root / "raw")
        item = store.upsert_canonical_item(
            CanonicalItem(source="hatena", source_id="hatena-1", content_key="url:one", url="https://example.test/a", title="Article")
        )
        store.record_resource_revision(item.resource_id or "", content_markdown="Body")

        report = render_raw_views(store, root, config, apply=True, replace_legacy=True)

        assert report.written == 1
        assert report.conflicts == 0
        assert "feedian_managed: true" in legacy.read_text(encoding="utf-8")
    finally:
        store.close()


def test_apply_refuses_legacy_replacement_after_human_edit(tmp_path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    initialize_vault(root)
    config = VaultConfig(providers={"hatena": VaultConfig().providers["hatena"]})
    legacy = root / "raw" / "Hatena" / "Article - hatena-1.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"legacy\n")
    store = VaultStore.open(root / ".feedian" / "feedian.sqlite3")
    try:
        store.import_legacy_artifacts(root / "raw")
        legacy.write_bytes(b"human edit\n")
        item = store.upsert_canonical_item(
            CanonicalItem(source="hatena", source_id="hatena-1", content_key="url:one", url="https://example.test/a", title="Article")
        )
        store.record_resource_revision(item.resource_id or "", content_markdown="Body")

        report = render_raw_views(store, root, config, apply=True, replace_legacy=True)

        assert report.conflicts == 1
        assert legacy.read_bytes() == b"human edit\n"
    finally:
        store.close()
