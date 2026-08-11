from __future__ import annotations

from feedian.canonical import CanonicalItem
from feedian.ingest import ingest_source_notes, render_source_notes
from feedian.store import VaultStore
from feedian.vault import VaultConfig, initialize_vault


def test_ingest_reuses_stored_llm_result_without_calling_api(tmp_path, monkeypatch) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    initialize_vault(root)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    store = VaultStore.open(root / ".feedian" / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(
            CanonicalItem(source="hatena", source_id="one", content_key="url:one", url="https://example.test", title="Article")
        )
        revision, _ = store.record_resource_revision(item.resource_id or "", content_markdown="Body", title="Article")
        # This exact request fingerprint is exercised through an initial fake API call.
        class Audit:
            result = {"note_title": "Summary", "summary": "Short", "key_points": ["Point"], "tags": ["tag"], "content_type": "article"}
            request = {}
            response = {"id": "response"}
            usage = {"input_tokens": 1, "output_tokens": 1}

        monkeypatch.setattr("feedian.ingest.summarize_bookmark_with_audit", lambda *args, **kwargs: Audit())
        first = ingest_source_notes(store, root, VaultConfig(), model="gpt-5.6-terra")
        second = ingest_source_notes(store, root, VaultConfig(), model="gpt-5.6-terra")
        written, skipped = render_source_notes(store, root, VaultConfig())

        assert first.created == 1
        assert second.reused == 1
        assert written == 1
        assert skipped == 0
        assert "## Summary" in next((root / "source").glob("*.md")).read_text(encoding="utf-8")
    finally:
        store.close()
