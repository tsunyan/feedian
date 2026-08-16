from __future__ import annotations

import json

from feedian.canonical import CanonicalItem
from feedian.ingest import ingest_source_notes, plan_source_notes, render_source_notes
from feedian.llm_backends import BackendAudit, BackendCapabilities
from feedian.store import VaultStore
from feedian.vault import VaultConfig, initialize_vault


class FakeBackend:
    def __init__(
        self,
        audit,
        *,
        backend: str = "openai-responses",
        billing_mode: str = "metered-api",
        error: Exception | None = None,
    ) -> None:
        self.audit = audit
        self.error = error
        self.capabilities = BackendCapabilities(
            backend=backend,
            auth_mode="api-key",
            billing_mode=billing_mode,
            max_article_chars=3_000 if backend == "manus-api" else 10_000,
            usage_available=bool(getattr(audit, "usage", {})),
        )

    def default_model(self) -> str:
        return "model"

    def preflight(self):
        return {"implementation_revision": "test"}

    def summarize(self, **_kwargs):
        if self.error is not None:
            raise self.error
        return BackendAudit(
            result=self.audit.result,
            request=self.audit.request,
            response=self.audit.response,
            usage=self.audit.usage,
            auth_mode=self.capabilities.auth_mode,
            billing_mode=self.capabilities.billing_mode,
            metadata={"implementation_revision": "test"},
        )


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

        backend = FakeBackend(Audit())
        progress = []
        first = ingest_source_notes(
            store, root, VaultConfig(), model="gpt-5.6-terra",
            progress=lambda *event: progress.append(event), backend_instance=backend,
        )
        second = ingest_source_notes(
            store, root, VaultConfig(), model="gpt-5.6-terra", backend_instance=backend
        )
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
        backend = FakeBackend(None, error=AssertionError("API called"))

        plan = plan_source_notes(store, model="gpt-5.6-terra", backend_instance=backend)
        report = ingest_source_notes(
            store, root, VaultConfig(), model="gpt-5.6-terra", dry_run=True,
            backend_instance=backend,
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

        backend = FakeBackend(Audit())
        ingest_source_notes(
            store, root, VaultConfig(), model="gpt-5.6-terra", backend_instance=backend
        )
        second_item = store.upsert_canonical_item(
            CanonicalItem(
                source="hatena", source_id="two", content_key="url:two",
                url="https://example.test/two", title="Two",
            )
        )
        store.record_resource_revision(second_item.resource_id or "", content_markdown="Two body", title="Two")

        plan = plan_source_notes(store, model="gpt-5.6-terra", backend_instance=backend)

        assert plan.usage_records == 1
        assert plan.estimated_output_tokens is not None
        assert plan.estimated_cost_usd is not None
        assert plan.estimated_cost_usd < (plan.max_cost_usd or 0)
    finally:
        store.close()


def test_llm_run_records_the_request_that_was_actually_sent(tmp_path, monkeypatch) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    initialize_vault(root)
    monkeypatch.setenv("MANUS_API_KEY", "test-key")
    store = VaultStore.open(root / ".feedian" / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(
            CanonicalItem(
                source="hatena", source_id="one", content_key="url:one",
                url="https://example.test", title="Article",
            )
        )
        store.record_resource_revision(item.resource_id or "", content_markdown="Body", title="Article")

        class Audit:
            result = {"note_title": "One", "summary": "Short", "key_points": [], "tags": ["one"], "content_type": "article"}
            # A Manus payload has a different shape from the planned OpenAI request.
            request = {"message": {"content": "sent to manus"}, "agent_profile": "manus-1.6"}
            response = {"id": "response"}
            usage: dict[str, int] = {}

        backend = FakeBackend(Audit(), backend="manus-api")
        ingest_source_notes(
            store, root, VaultConfig(), model="manus-1.6", provider="manus",
            backend_instance=backend,
        )

        stored = store.connection.execute("SELECT request_json FROM llm_run").fetchone()[0]
        assert "sent to manus" in stored
        assert "input_text" not in stored
    finally:
        store.close()


def test_failed_run_keeps_the_request_it_started_with(tmp_path, monkeypatch) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    initialize_vault(root)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    store = VaultStore.open(root / ".feedian" / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(
            CanonicalItem(
                source="hatena", source_id="one", content_key="url:one",
                url="https://example.test", title="Article",
            )
        )
        store.record_resource_revision(item.resource_id or "", content_markdown="Body", title="Article")
        backend = FakeBackend(None, error=RuntimeError("boom"))

        report = ingest_source_notes(
            store, root, VaultConfig(), model="gpt-5.6-terra", backend_instance=backend
        )

        row = store.connection.execute("SELECT status, request_json FROM llm_run").fetchone()
        assert report.failed == 1
        assert row[0] == "failed"
        assert "input_text" in row[1]
    finally:
        store.close()


def test_ingest_counts_requests_it_cannot_price_or_meter(tmp_path, monkeypatch) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    initialize_vault(root)
    monkeypatch.setenv("MANUS_API_KEY", "test-key")
    store = VaultStore.open(root / ".feedian" / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(
            CanonicalItem(
                source="hatena", source_id="one", content_key="url:one",
                url="https://example.test", title="Article",
            )
        )
        store.record_resource_revision(item.resource_id or "", content_markdown="Body", title="Article")

        class Audit:
            result = {"note_title": "One", "summary": "Short", "key_points": [], "tags": ["one"], "content_type": "article"}
            request = {}
            response = {"id": "response"}
            usage: dict[str, int] = {}

        backend = FakeBackend(Audit(), backend="manus-api")

        plan = plan_source_notes(
            store, model="manus-1.6", provider="manus", backend_instance=backend
        )
        report = ingest_source_notes(
            store, root, VaultConfig(), model="manus-1.6", provider="manus", plan=plan,
            backend_instance=backend,
        )

        # A provider Feedian cannot price must not read as a completed free run.
        assert plan.max_cost_usd is None
        assert report.created == 1
        assert report.cost_usd == 0.0
        assert report.unpriced_requests == 1
        assert report.unmetered_requests == 1
        row = store.connection.execute(
            "SELECT backend, auth_mode, billing_mode, usage_json FROM llm_run"
        ).fetchone()
        assert tuple(row) == ("manus-api", "api-key", "metered-api", None)
    finally:
        store.close()


def test_results_are_not_reused_across_backend_boundaries(tmp_path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    initialize_vault(root)
    store = VaultStore.open(root / ".feedian" / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(
            CanonicalItem(
                source="hatena", source_id="one", content_key="url:one",
                url="https://example.test", title="Article",
            )
        )
        store.record_resource_revision(item.resource_id or "", content_markdown="Body", title="Article")

        class Audit:
            result = {"note_title": "One", "summary": "Short", "key_points": [], "tags": [], "content_type": ""}
            request = {}
            response = {}
            usage = {"input_tokens": 1, "output_tokens": 1}

        openai = FakeBackend(Audit())
        ingest_source_notes(
            store, root, VaultConfig(), model="same-model", backend_instance=openai
        )
        manus = FakeBackend(Audit(), backend="manus-api")
        plan = plan_source_notes(
            store, model="same-model", backend="manus-api", backend_instance=manus
        )

        assert plan.new_requests == 1
        assert plan.reusable == 0
    finally:
        store.close()


def test_legacy_fingerprint_is_reused_and_promoted_without_an_api_call(tmp_path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    initialize_vault(root)
    store = VaultStore.open(root / ".feedian" / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(
            CanonicalItem(
                source="hatena", source_id="one", content_key="url:one",
                url="https://example.test", title="Article",
            )
        )
        revision, _ = store.record_resource_revision(
            item.resource_id or "", content_markdown="Body", title="Article"
        )

        class Audit:
            result = {"note_title": "One", "summary": "Short", "key_points": [], "tags": [], "content_type": ""}
            request = {}
            response = {}
            usage = {}

        backend = FakeBackend(Audit())
        candidate = plan_source_notes(
            store, model="gpt-test", force=True, backend_instance=backend
        ).candidates[0]
        run_id = store.start_llm_run(
            resource_id=item.resource_id or "",
            resource_revision_id=revision,
            operation="source-note",
            model="gpt-test",
            prompt_version="source-note-v1",
            input_fingerprint=candidate.legacy_fingerprint,
            request={"provider": "openai"},
            backend="openai-responses",
            summary_schema_version="1",
            fingerprint_version=1,
            auth_mode="api-key",
            billing_mode="metered-api",
        )
        store.finish_llm_run(run_id, result=Audit.result)

        plan = plan_source_notes(store, model="gpt-test", backend_instance=backend)
        planned = store.connection.execute(
            "SELECT fingerprint_version, input_fingerprint FROM llm_run WHERE llm_run_id = ?",
            (run_id,),
        ).fetchone()

        assert plan.reusable == 1
        # Planning runs outside the write lock, so it must leave the row alone.
        assert tuple(planned) == (1, candidate.legacy_fingerprint)

        ingest_source_notes(
            store, root, VaultConfig(), model="gpt-test", plan=plan, backend_instance=backend,
        )
        migrated = store.connection.execute(
            """
            SELECT fingerprint_version, input_fingerprint, backend_metadata_json
            FROM llm_run WHERE llm_run_id = ?
            """,
            (run_id,),
        ).fetchone()

        assert tuple(migrated[:2]) == (2, candidate.fingerprint)
        assert json.loads(migrated[2])["legacy_fingerprint_promoted"] is True
    finally:
        store.close()


def test_legacy_fingerprint_matches_the_key_the_previous_release_stored(tmp_path) -> None:
    """Pin the version-one reuse key to the value 2385ec2 actually wrote.

    The key hashes the whole request, so any edit to the summary schema silently
    stops the migration window from finding reusable results. This literal was
    computed by running that commit's build_summary_request over this fixture.
    """

    root = tmp_path / "vault"
    root.mkdir()
    initialize_vault(root)
    store = VaultStore.open(root / ".feedian" / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(
            CanonicalItem(
                source="hatena", source_id="one", content_key="url:one",
                url="https://example.test", title="Article",
            )
        )
        store.record_resource_revision(
            item.resource_id or "", content_markdown="Body", title="Article"
        )

        candidate = plan_source_notes(
            store, model="gpt-test", backend_instance=FakeBackend(None)
        ).candidates[0]

        assert candidate.legacy_fingerprint == (
            "b2cccd34551a6f6eefca14a5a31af494413d33ee4f84c2fd3bedd781f088cd95"
        )
    finally:
        store.close()
