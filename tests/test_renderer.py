from __future__ import annotations

from feedian.canonical import CanonicalItem
from feedian.renderer import _is_legacy_generated_document, render_raw_views
from feedian.store import VaultStore
from feedian.vault import VaultConfig, initialize_vault


def test_renderer_stages_raw_and_comments_without_touching_raw(tmp_path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    initialize_vault(root)
    stale_asset = root / ".feedian" / "staging" / "raw" / "assets" / "stale.png"
    stale_asset.parent.mkdir(parents=True)
    stale_asset.write_bytes(b"stale")
    config = VaultConfig(providers={"hatena": VaultConfig().providers["hatena"]})
    store = VaultStore.open(root / ".feedian" / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(
            CanonicalItem(source="hatena", source_id="hatena-1", content_key="url:one", url="https://example.test/a", title="Article")
        )
        revision, _ = store.record_resource_revision(item.resource_id or "", content_markdown="Body", discussion_text="Reply")
        store.replace_resource_images(
            resource_id=item.resource_id or "",
            resource_revision_id=revision,
            images=[("https://example.test/image.png", "Example image")],
        )
        store.upsert_comment(provider="hatena", resource_id=item.resource_id or "", author="alice", body="Useful", star_count=3)

        progress: list[tuple[int, int]] = []
        report = render_raw_views(
            store,
            root,
            config,
            progress=lambda processed, total: progress.append((processed, total)),
        )

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
        assert "![Example image](<https://example.test/image.png>)" in staged.read_text(encoding="utf-8")
        assert not (root / ".feedian" / "staging" / "raw" / "assets").exists()
        assert "★3" in comments.read_text(encoding="utf-8")
        assert progress == [(0, 1), (1, 1)]
    finally:
        store.close()


def test_only_recognizes_legacy_feedian_documents_with_all_identity_fields() -> None:
    legacy = """---
source_type: "hatena"
source_id: "hatena-abc123"
content_key: "url:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
---

# Generated
"""

    assert _is_legacy_generated_document(legacy) is True
    assert _is_legacy_generated_document(legacy.replace("content_key:", "other_key:")) is False
    assert _is_legacy_generated_document("# Handwritten\n") is False

    old_raindrop = """---
source: "https://example.test/article"
raindrop_id: "123"
raindrop_collection_id: "-1"
summary_generated_at: "2026-08-09T00:00:00+00:00"
summary_model: "gpt-test"
---

# Generated
"""
    assert _is_legacy_generated_document(old_raindrop) is True
    assert _is_legacy_generated_document(old_raindrop.replace("summary_model:", "model:")) is False


def test_apply_reconciles_a_legacy_raindrop_title_path(tmp_path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    initialize_vault(root)
    config = VaultConfig(providers={"raindrop": VaultConfig().providers["raindrop"]})
    old_path = root / "raw" / "Raindrop" / "Old title - 123.md"
    old_path.parent.mkdir(parents=True)
    old_path.write_text(
        """---
source: "https://example.test/article"
raindrop_id: "123"
raindrop_collection_id: "-1"
summary_generated_at: "2026-08-09T00:00:00+00:00"
summary_model: "gpt-test"
---

# Old title
""",
        encoding="utf-8",
    )
    store = VaultStore.open(root / ".feedian" / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(
            CanonicalItem(
                source="raindrop",
                source_id="123",
                content_key="url:one",
                url="https://example.test/article",
                title="New title",
            )
        )
        store.record_resource_revision(item.resource_id or "", content_markdown="Body")

        report = render_raw_views(store, root, config, apply=True)

        new_path = root / "raw" / "Raindrop" / "New title - 123.md"
        assert report.conflicts == 0
        assert not old_path.exists()
        assert new_path.exists()
        assert "feedian_managed: true" in new_path.read_text(encoding="utf-8")
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
