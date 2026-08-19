from __future__ import annotations

import json
import threading
import time
from dataclasses import replace

import pytest

from feedian.canonical import CanonicalItem
from feedian.ingest import (
    fallback_maximum_cost,
    ingest_source_notes,
    plan_source_notes,
    render_source_notes,
    resolve_fallback,
)
from feedian.llm import PROVIDER_OUTPUT_SCHEMA
from feedian.llm_backends import (
    BackendAudit,
    BackendAuthError,
    BackendCapabilities,
    BackendPolicyError,
    BackendRateLimitError,
)
from feedian.store import VaultStore
from feedian.vault import LLMFallbackSettings, VaultConfig, initialize_vault


class FakeBackend:
    def __init__(
        self,
        audit,
        *,
        backend: str = "openai-responses",
        auth_mode: str = "api-key",
        execution_kind: str = "http",
        billing_mode: str = "metered-api",
        error: Exception | None = None,
    ) -> None:
        self.audit = audit
        self.error = error
        self.temporary_parent = None
        self.summarize_kwargs = {}
        self.capabilities = BackendCapabilities(
            backend=backend,
            execution_kind=execution_kind,
            auth_mode=auth_mode,
            billing_mode=billing_mode,
            max_article_chars=3_000 if backend == "manus-api" else 10_000,
            usage_available=bool(getattr(audit, "usage", {})),
        )

    def default_model(self) -> str:
        return "model"

    def supports_model(self, model: str) -> bool:
        del model
        return True

    def preflight(self):
        return {"implementation_revision": "test"}

    def summarize(self, **kwargs):
        self.summarize_kwargs = kwargs
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

        stored = json.loads(
            store.connection.execute("SELECT request_json FROM llm_run").fetchone()[0]
        )
        assert stored["actual"]["message"]["content"] == "sent to manus"
        assert stored["logical"]["input"][0]["content"][0]["type"] == "input_text"
    finally:
        store.close()


def test_failed_run_keeps_a_stable_logical_and_actual_request_envelope(tmp_path, monkeypatch) -> None:
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
        request = json.loads(row[1])
        assert request["logical"]["input"][0]["content"][0]["type"] == "input_text"
        assert request["actual"] is None
    finally:
        store.close()


def test_local_backend_runs_outside_the_vault_project(tmp_path, monkeypatch) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    initialize_vault(root)
    (root / ".git").mkdir()
    (root / "AGENTS.md").write_text("project instructions", encoding="utf-8")
    system_temp = tmp_path / "system-temp"
    system_temp.mkdir()
    monkeypatch.setattr("feedian.local_agent.tempfile.gettempdir", lambda: str(system_temp))
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
            result = {
                "note_title": "One", "summary": "Short", "key_points": [],
                "tags": [], "content_type": "",
            }
            request = {"mode": "stdin", "argv": ["codex-test", "exec", "-"]}
            response = {}
            usage = {}

        backend = FakeBackend(
            Audit(), backend="codex-local", auth_mode="local-session", execution_kind="local-agent",
            billing_mode="subscription",
        )
        ingest_source_notes(
            store, root, VaultConfig(), model="gpt-test", backend="codex-local",
            backend_instance=backend,
        )

        assert backend.summarize_kwargs["temporary_parent"] == system_temp.resolve()
        assert root.resolve() not in system_temp.resolve().parents
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


def test_legacy_fingerprint_is_isolated_from_provider_schema_changes(tmp_path) -> None:
    """Changing what providers are asked for must not end the migration window.

    The current key covers the whole request and is meant to move; the version-one
    key is rebuilt from a frozen schema and must not.
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

        def plan() -> object:
            return plan_source_notes(
                store, model="gpt-test", backend_instance=FakeBackend(None)
            ).candidates[0]

        before = plan()
        original = PROVIDER_OUTPUT_SCHEMA["properties"]["tags"]["minItems"]
        PROVIDER_OUTPUT_SCHEMA["properties"]["tags"]["minItems"] = 2
        try:
            after = plan()
        finally:
            PROVIDER_OUTPUT_SCHEMA["properties"]["tags"]["minItems"] = original

        assert after.fingerprint != before.fingerprint
        assert after.legacy_fingerprint == before.legacy_fingerprint
    finally:
        store.close()


def _vault_with_one_article(root):
    initialize_vault(root)
    store = VaultStore.open(root / ".feedian" / "feedian.sqlite3")
    item = store.upsert_canonical_item(
        CanonicalItem(
            source="hatena", source_id="one", content_key="url:one",
            url="https://example.test", title="Article",
        )
    )
    store.record_resource_revision(
        item.resource_id or "", content_markdown="Body", title="Article"
    )
    return store


class _Audit:
    result = {
        "note_title": "One", "summary": "Short", "key_points": [],
        "tags": ["one"], "content_type": "article",
    }
    request = {}
    response = {"id": "response"}
    usage = {"input_tokens": 5, "output_tokens": 2}


def test_enabled_fallback_runs_the_named_backend_and_records_it_separately(tmp_path, monkeypatch) -> None:
    """An allowance that runs out is why fallback exists; the audit must show both."""

    root = tmp_path / "vault"
    root.mkdir()
    # isolated_local_agent_parent walks the system temp directory's ancestors for
    # .git, so an unpinned TMPDIR under version control fails before the assertion.
    system_temp = tmp_path / "system-temp"
    system_temp.mkdir()
    monkeypatch.setattr("feedian.local_agent.tempfile.gettempdir", lambda: str(system_temp))
    store = _vault_with_one_article(root)
    try:
        config = VaultConfig()
        config.llm.fallback = LLMFallbackSettings(
            enabled=True, backend="openai-responses", model="gpt-5.6-terra"
        )
        primary = FakeBackend(
            None, backend="codex-local", execution_kind="local-agent",
            auth_mode="local-session", billing_mode="subscription",
            error=BackendRateLimitError("weekly allowance reached"),
        )
        fallback = FakeBackend(_Audit())

        report = ingest_source_notes(
            store, root, config, model="gpt-test", backend="codex-local",
            backend_instance=primary, fallback_instance=fallback,
        )

        rows = store.connection.execute(
            "SELECT backend, model, status FROM llm_run ORDER BY started_at"
        ).fetchall()
        assert report.created == 1
        assert report.failed == 0
        assert [tuple(row) for row in rows] == [
            ("codex-local", "gpt-test", "failed"),
            ("openai-responses", "gpt-5.6-terra", "completed"),
        ]
    finally:
        store.close()


def test_fallback_does_not_rescue_a_credential_or_policy_failure(tmp_path) -> None:
    """A rejected credential is a fault to fix, not a reason to start billing."""

    root = tmp_path / "vault"
    root.mkdir()
    store = _vault_with_one_article(root)
    try:
        config = VaultConfig()
        config.llm.fallback = LLMFallbackSettings(
            enabled=True, backend="openai-responses", model="gpt-5.6-terra"
        )
        primary = FakeBackend(
            None, backend="codex-local", execution_kind="local-agent",
            auth_mode="local-session", billing_mode="subscription",
            error=BackendAuthError("not logged in"),
        )
        fallback = FakeBackend(_Audit(), error=AssertionError("fallback must not run"))

        report = ingest_source_notes(
            store, root, config, model="gpt-test", backend="codex-local",
            backend_instance=primary, fallback_instance=fallback,
        )

        assert report.failed == 1
        assert report.created == 0
        assert store.connection.execute("SELECT COUNT(*) FROM llm_run").fetchone()[0] == 1
    finally:
        store.close()


def test_fallback_stays_off_unless_the_config_enables_it(tmp_path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    store = _vault_with_one_article(root)
    try:
        primary = FakeBackend(None, error=BackendRateLimitError("429"))
        fallback = FakeBackend(_Audit(), error=AssertionError("fallback must not run"))

        report = ingest_source_notes(
            store, root, VaultConfig(), model="gpt-test", backend_instance=primary,
            fallback_instance=fallback,
        )

        assert report.failed == 1
    finally:
        store.close()


def test_a_local_fallback_runs_outside_the_vault_even_behind_an_http_primary(tmp_path, monkeypatch) -> None:
    """The primary picks no isolation root for the fallback; the fallback does.

    An HTTP primary leaves temporary_parent inside the Vault, and Codex treats
    its cwd as a project, so running there would reinstate project instructions.
    """

    root = tmp_path / "vault"
    root.mkdir()
    # isolated_local_agent_parent walks the system temp directory's ancestors for
    # .git, so an unpinned TMPDIR under version control fails before the assertion.
    system_temp = tmp_path / "system-temp"
    system_temp.mkdir()
    monkeypatch.setattr("feedian.local_agent.tempfile.gettempdir", lambda: str(system_temp))
    store = _vault_with_one_article(root)
    try:
        config = VaultConfig()
        config.llm.fallback = LLMFallbackSettings(
            enabled=True, backend="codex-local", model="gpt-test"
        )
        primary = FakeBackend(None, error=BackendRateLimitError("429"))
        fallback = FakeBackend(
            _Audit(), backend="codex-local", execution_kind="local-agent",
            auth_mode="local-session", billing_mode="subscription",
        )

        ingest_source_notes(
            store, root, config, model="gpt-test", backend_instance=primary,
            fallback_instance=fallback,
        )

        used = fallback.summarize_kwargs["temporary_parent"].resolve()
        # Path.parents excludes the path itself, so the Vault root would slip past.
        assert used != root.resolve()
        assert root.resolve() not in used.parents
        assert used != (root / ".feedian" / "tmp").resolve()
    finally:
        store.close()


def test_a_fallback_model_the_destination_cannot_serve_is_refused(tmp_path) -> None:
    """Recorded provenance would otherwise name a model that never ran."""

    config = VaultConfig()
    config.llm.fallback = LLMFallbackSettings(
        enabled=True, backend="manus-api", model="gpt-5.6-terra"
    )

    with pytest.raises(BackendPolicyError, match="does not support model"):
        resolve_fallback(config, "openai-responses")


def test_the_plan_states_what_an_enabled_metered_fallback_could_cost(tmp_path) -> None:
    """A subscription primary reports no cost, so the fallback states its own."""

    root = tmp_path / "vault"
    root.mkdir()
    store = _vault_with_one_article(root)
    try:
        subscription = FakeBackend(
            None, backend="codex-local", execution_kind="local-agent",
            auth_mode="local-session", billing_mode="subscription",
        )
        plan = plan_source_notes(
            store, model="gpt-5.6-terra", backend="codex-local", backend_instance=subscription,
        )
        metered = FakeBackend(_Audit())
        config = VaultConfig()
        config.llm.fallback = LLMFallbackSettings(
            enabled=True, backend="openai-responses", model="gpt-5.6-terra"
        )
        fallback = resolve_fallback(config, "codex-local", backend_instance=metered)

        assert plan.max_cost_usd is None
        assert fallback_maximum_cost(plan, fallback) > 0
    finally:
        store.close()


# --- Parallel ingest. Spec 20260819-sync-ingest-throughput. ---


class _Audit:
    result = {
        "note_title": "Summary", "summary": "Short", "key_points": ["Point"],
        "tags": ["tag"], "content_type": "article",
    }
    request: dict = {}
    response = {"id": "response"}
    usage = {"input_tokens": 1, "output_tokens": 1}


class ConcurrencyProbe(FakeBackend):
    """Records how many summarize calls overlap, and when each one started."""

    def __init__(self, *, max_parallelism: int = 8, min_start_interval_seconds: float = 0.0,
                 hold: float = 0.02, **kwargs) -> None:
        super().__init__(_Audit(), **kwargs)
        self.capabilities = replace(
            self.capabilities,
            max_parallelism=max_parallelism,
            min_start_interval_seconds=min_start_interval_seconds,
        )
        self.hold = hold
        self.peak = 0
        self.starts: list[float] = []
        self._live = 0
        self._lock = threading.Lock()

    def summarize(self, **kwargs):
        with self._lock:
            self._live += 1
            self.peak = max(self.peak, self._live)
            self.starts.append(time.monotonic())
        try:
            if self.hold > 0:
                time.sleep(self.hold)
            return super().summarize(**kwargs)
        finally:
            with self._lock:
                self._live -= 1


def _vault_with_resources(root, count: int):
    initialize_vault(root)
    store = VaultStore.open(root / ".feedian" / "feedian.sqlite3")
    for index in range(count):
        item = store.upsert_canonical_item(
            CanonicalItem(
                source="hatena", source_id=f"item-{index}", content_key=f"url:{index}",
                url=f"https://example.test/{index}", title=f"Article {index}",
            )
        )
        store.record_resource_revision(item.resource_id or "", content_markdown=f"Body {index}", title=f"A{index}")
    return store


@pytest.mark.parametrize("workers,expected_peak", [(1, 1), (3, 3)])
def test_llm_workers_bounds_how_many_summaries_overlap(tmp_path, monkeypatch, workers, expected_peak) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    root = tmp_path / "vault"
    root.mkdir()
    store = _vault_with_resources(root, 6)
    try:
        backend = ConcurrencyProbe()
        config = VaultConfig()
        config.llm.workers = workers
        report = ingest_source_notes(
            store, root, config, model="gpt-5.6-terra", backend_instance=backend
        )

        assert report.created == 6
        assert backend.peak == expected_peak
    finally:
        store.close()


def test_a_backend_that_declares_one_stays_serial_however_many_workers(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    root = tmp_path / "vault"
    root.mkdir()
    store = _vault_with_resources(root, 4)
    try:
        backend = ConcurrencyProbe(max_parallelism=1)
        config = VaultConfig()
        config.llm.workers = 8
        ingest_source_notes(store, root, config, model="gpt-5.6-terra", backend_instance=backend)

        assert backend.peak == 1, "codex-local declares 1 for the same reason"
    finally:
        store.close()


def test_the_start_interval_paces_the_run(tmp_path, monkeypatch) -> None:
    """What is guaranteed is the spacing of submissions, not of wire sends.

    Between the scheduler admitting a job and the worker reaching the provider
    there is thread dispatch, so per-call gaps jitter either way. Three jobs at a
    0.05s interval still cannot finish in under two intervals.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    root = tmp_path / "vault"
    root.mkdir()
    store = _vault_with_resources(root, 3)
    try:
        backend = ConcurrencyProbe(min_start_interval_seconds=0.05, hold=0.0)
        config = VaultConfig()
        config.llm.workers = 8
        started = time.monotonic()
        report = ingest_source_notes(
            store, root, config, model="gpt-5.6-terra", backend_instance=backend
        )
        elapsed = time.monotonic() - started

        assert report.created == 3
        assert len(backend.starts) == 3
        assert elapsed >= 0.10, f"three starts at 0.05s apart took {elapsed:.3f}s"
    finally:
        store.close()


def test_an_interrupt_closes_every_run_and_starts_no_further_backend_call(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    root = tmp_path / "vault"
    root.mkdir()
    store = _vault_with_resources(root, 6)
    try:
        class Interrupting(ConcurrencyProbe):
            def summarize(self, **kwargs):
                with self._lock:
                    self.starts.append(time.monotonic())
                if len(self.starts) == 1:
                    raise KeyboardInterrupt
                return FakeBackend.summarize(self, **kwargs)

        backend = Interrupting(max_parallelism=1)
        config = VaultConfig()
        config.llm.workers = 1
        with pytest.raises(KeyboardInterrupt):
            ingest_source_notes(store, root, config, model="gpt-5.6-terra", backend_instance=backend)

        left_running = store.connection.execute(
            "SELECT COUNT(*) FROM llm_run WHERE status = 'running'"
        ).fetchone()[0]
        assert left_running == 0, "no run is left open by a graceful stop"
        assert len(backend.starts) == 1, "nothing new is sent after the interrupt"
    finally:
        store.close()


def test_runs_left_running_by_a_killed_process_are_closed_on_the_next_ingest(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    root = tmp_path / "vault"
    root.mkdir()
    store = _vault_with_resources(root, 1)
    try:
        item = store.connection.execute("SELECT resource_id FROM resource LIMIT 1").fetchone()[0]
        revision = store.connection.execute(
            "SELECT resource_revision_id FROM resource_revision LIMIT 1"
        ).fetchone()[0]
        store.start_llm_run(
            resource_id=item, resource_revision_id=revision, operation="source-note",
            model="m", prompt_version="v", input_fingerprint="abandoned",
            request={"logical": {}, "actual": None}, backend="openai-responses",
            summary_schema_version=1, fingerprint_version=2,
            auth_mode="api-key", billing_mode="metered-api", backend_metadata={},
        )
        assert store.connection.execute(
            "SELECT COUNT(*) FROM llm_run WHERE status = 'running'"
        ).fetchone()[0] == 1

        ingest_source_notes(
            store, root, VaultConfig(), model="gpt-5.6-terra", backend_instance=ConcurrencyProbe()
        )

        assert store.connection.execute(
            "SELECT COUNT(*) FROM llm_run WHERE status = 'running'"
        ).fetchone()[0] == 0
    finally:
        store.close()


def test_the_scheduler_waits_instead_of_spinning_when_nothing_may_start_yet(tmp_path, monkeypatch) -> None:
    """Waiting on an empty set of Futures returns at once; without this it spins."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    root = tmp_path / "vault"
    root.mkdir()
    store = _vault_with_resources(root, 2)
    slept: list[float] = []
    real_sleep = time.sleep

    def record(seconds):
        slept.append(seconds)
        real_sleep(seconds)

    try:
        backend = ConcurrencyProbe(max_parallelism=1, min_start_interval_seconds=0.05, hold=0.0)
        config = VaultConfig()
        config.llm.workers = 1
        monkeypatch.setattr("feedian.ingest.time.sleep", record)
        ingest_source_notes(store, root, config, model="gpt-5.6-terra", backend_instance=backend)

        assert slept, "the second job is not due yet and nothing is running, so it waits"
        assert all(seconds > 0 for seconds in slept)
        assert len(slept) < 10, f"one wait per opening, not a spin: {slept}"
    finally:
        store.close()


def test_a_fallback_passes_through_its_own_backend_limits(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("MANUS_API_KEY", "manus-key")
    root = tmp_path / "vault"
    root.mkdir()
    store = _vault_with_resources(root, 4)
    try:
        primary = ConcurrencyProbe(error=BackendRateLimitError("slow down"))
        secondary = ConcurrencyProbe(backend="manus-api", max_parallelism=1, hold=0.02)
        config = VaultConfig()
        config.llm.workers = 8
        config.llm.fallback = LLMFallbackSettings(enabled=True, backend="manus-api", model="manus-1.6")
        report = ingest_source_notes(
            store, root, config, model="gpt-5.6-terra",
            backend_instance=primary, fallback_instance=secondary,
        )

        assert report.created == 4, "every article was summarised by the fallback"
        assert secondary.peak == 1, "the fallback obeys its own limit, not the primary's"
    finally:
        store.close()


def test_a_fallback_counts_as_one_candidate_not_two(tmp_path, monkeypatch) -> None:
    """Review 20260819-2: a fallback is a second attempt, not a second candidate."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("MANUS_API_KEY", "manus-key")
    root = tmp_path / "vault"
    root.mkdir()
    store = _vault_with_resources(root, 1)
    try:
        config = VaultConfig()
        config.llm.fallback = LLMFallbackSettings(enabled=True, backend="manus-api", model="manus-1.6")
        seen: list[tuple[int, int]] = []
        report = ingest_source_notes(
            store, root, config, model="gpt-5.6-terra",
            backend_instance=ConcurrencyProbe(error=BackendRateLimitError("slow down")),
            fallback_instance=ConcurrencyProbe(backend="manus-api"),
            progress=lambda processed, total, _candidate, _report: seen.append((processed, total)),
        )

        assert report.created == 1
        assert report.processed == 1
        assert all(processed <= total for processed, total in seen), seen
    finally:
        store.close()


def test_progress_counts_finished_candidates_not_submitted_ones(tmp_path, monkeypatch) -> None:
    """Review 20260819-4: jobs are submitted ahead of the first result.

    Counting at submit made every candidate look done as soon as one was.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    root = tmp_path / "vault"
    root.mkdir()
    store = _vault_with_resources(root, 3)
    try:
        config = VaultConfig()
        config.llm.workers = 3
        seen: list[tuple[int, int]] = []
        report = ingest_source_notes(
            store, root, config, model="gpt-5.6-terra",
            backend_instance=ConcurrencyProbe(),
            progress=lambda processed, total, _candidate, _report: seen.append((processed, total)),
        )

        assert report.processed == 3
        assert seen == [(1, 3), (2, 3), (3, 3)]
    finally:
        store.close()


def test_a_dry_run_leaves_another_process_running_llm_run_alone(tmp_path, monkeypatch) -> None:
    """Review 20260819-6: planning happens without the vault write lock.

    The CLI plans a dry run before taking the lock, so nothing on that path may
    write - least of all close a run another ingest is still executing.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    root = tmp_path / "vault"
    root.mkdir()
    store = _vault_with_resources(root, 1)
    try:
        resource_id = store.connection.execute("SELECT resource_id FROM resource LIMIT 1").fetchone()[0]
        revision_id = store.connection.execute(
            "SELECT resource_revision_id FROM resource_revision LIMIT 1"
        ).fetchone()[0]
        store.start_llm_run(
            resource_id=resource_id, resource_revision_id=revision_id, operation="source-note",
            model="m", prompt_version="v", input_fingerprint="owned-by-another-process",
            request={"logical": {}, "actual": None}, backend="openai-responses",
            summary_schema_version=1, fingerprint_version=2,
            auth_mode="api-key", billing_mode="metered-api", backend_metadata={},
        )

        plan_source_notes(store, model="gpt-5.6-terra", backend_instance=ConcurrencyProbe())
        ingest_source_notes(
            store, root, VaultConfig(), model="gpt-5.6-terra", dry_run=True,
            backend_instance=ConcurrencyProbe(),
        )

        still_running = store.connection.execute(
            "SELECT COUNT(*) FROM llm_run WHERE status = 'running'"
        ).fetchone()[0]
        assert still_running == 1, "planning and a dry run must not touch it"
    finally:
        store.close()
