from __future__ import annotations

from feedian.canonical import CanonicalItem
from feedian.ingest import ingest_source_notes, plan_source_notes, render_source_notes
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
        progress = []
        first = ingest_source_notes(
            store, root, VaultConfig(), model="gpt-5.6-terra",
            progress=lambda *event: progress.append(event),
        )
        second = ingest_source_notes(store, root, VaultConfig(), model="gpt-5.6-terra")
        written, skipped = render_source_notes(store, root, VaultConfig())

        assert first.created == 1
        assert first.input_tokens == 1
        assert first.output_tokens == 1
        assert first.cost_usd > 0
        assert len(progress) == 1
        assert second.reused == 1
        assert written == 1
        assert skipped == 0
        assert "## Summary" in next((root / "source").glob("*.md")).read_text(encoding="utf-8")
    finally:
        store.close()


def test_ingest_dry_run_plans_without_api_key_or_writes(tmp_path, monkeypatch) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    initialize_vault(root)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    store = VaultStore.open(root / ".feedian" / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(
            CanonicalItem(
                source="hatena", source_id="one", content_key="url:one",
                url="https://example.test", title="Article", tags=["python"],
            )
        )
        store.record_resource_revision(item.resource_id or "", content_markdown="Body", title="Article")
        monkeypatch.setattr(
            "feedian.ingest.summarize_bookmark_with_audit",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("API called")),
        )

        plan = plan_source_notes(store, model="gpt-5.6-terra")
        report = ingest_source_notes(
            store, root, VaultConfig(), model="gpt-5.6-terra", dry_run=True,
        )

        assert len(plan.candidates) == 1
        assert plan.new_requests == 1
        assert plan.input_tokens > 0
        assert plan.max_cost_usd is not None
        assert report.processed == 1
        assert store.connection.execute("SELECT COUNT(*) FROM llm_run").fetchone()[0] == 0
        assert store.connection.execute("SELECT COUNT(*) FROM source_note").fetchone()[0] == 0
        assert not (root / "source").exists()
    finally:
        store.close()


def test_auto_ingest_prioritizes_an_uncovered_field(tmp_path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    initialize_vault(root)
    store = VaultStore.open(root / ".feedian" / "feedian.sqlite3")
    try:
        ai = store.upsert_canonical_item(
            CanonicalItem(
                source="hatena", source_id="ai", content_key="url:ai",
                url="https://example.test/ai", title="AI", tags=["ai"],
            )
        )
        store.record_resource_revision(ai.resource_id or "", content_markdown="AI body", title="AI")
        store.put_source_note(resource_id=ai.resource_id or "", llm_run_id=None, markdown="existing")
        python = store.upsert_canonical_item(
            CanonicalItem(
                source="hatena", source_id="python", content_key="url:python",
                url="https://example.test/python", title="Python", tags=["python"],
            )
        )
        store.record_resource_revision(
            python.resource_id or "", content_markdown="Longer Python body", title="Python",
        )

        plan = plan_source_notes(store, model="gpt-5.6-terra", auto=True, limit=1)

        assert len(plan.candidates) == 1
        assert plan.candidates[0].resource_id == python.resource_id
        assert plan.candidates[0].topic == "python"
        assert plan.candidates[0].reason == "uncovered-field"
    finally:
        store.close()


def test_plan_uses_historical_output_ratio_for_expected_cost(tmp_path, monkeypatch) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    initialize_vault(root)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    store = VaultStore.open(root / ".feedian" / "feedian.sqlite3")
    try:
        first_item = store.upsert_canonical_item(
            CanonicalItem(
                source="hatena", source_id="one", content_key="url:one",
                url="https://example.test/one", title="One",
            )
        )
        store.record_resource_revision(first_item.resource_id or "", content_markdown="One body", title="One")

        class Audit:
            result = {"note_title": "One", "summary": "Short", "key_points": [], "tags": ["one"], "content_type": "article"}
            request = {}
            response = {"id": "response"}
            usage = {"input_tokens": 1000, "output_tokens": 100}

        monkeypatch.setattr("feedian.ingest.summarize_bookmark_with_audit", lambda *args, **kwargs: Audit())
        ingest_source_notes(store, root, VaultConfig(), model="gpt-5.6-terra")
        second_item = store.upsert_canonical_item(
            CanonicalItem(
                source="hatena", source_id="two", content_key="url:two",
                url="https://example.test/two", title="Two",
            )
        )
        store.record_resource_revision(second_item.resource_id or "", content_markdown="Two body", title="Two")

        plan = plan_source_notes(store, model="gpt-5.6-terra")

        assert plan.usage_records == 1
        assert plan.estimated_output_tokens is not None
        assert plan.estimated_cost_usd is not None
        assert plan.estimated_cost_usd < (plan.max_cost_usd or 0)
    finally:
        store.close()
