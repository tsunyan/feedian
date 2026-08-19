from __future__ import annotations

import hashlib
import json
import re
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor
from concurrent.futures import wait as futures_wait
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .estimate import MODEL_PRICES, comparison_model, count_prompt_tokens, usage_cost_usd
from .extract import PageFetchResult
from .llm import (
    CANONICAL_SUMMARY_SCHEMA_VERSION,
    LEGACY_V1_PROVIDER_SCHEMA,
    SUMMARY_INSTRUCTIONS,
    build_summary_request,
)
from .llm_backends import BackendPolicyError, LLMBackend, canonical_backend_id, get_backend
from .local_agent import isolated_local_agent_parent, sanitize_error
from .markdown import escape_markdown_heading, sanitize_filename, yaml_frontmatter
from .store import VaultStore, stable_json
from .vault import VaultConfig, vault_paths


PROMPT_VERSION = "source-note-v1"


@dataclass(frozen=True)
class IngestReport:
    processed: int = 0
    created: int = 0
    reused: int = 0
    failed: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    # API calls that returned no price and no token usage. Without these, a
    # provider Feedian cannot price (Manus) is indistinguishable from a free run.
    unpriced_requests: int = 0
    unmetered_requests: int = 0
    last_status: str = ""
    last_run_id: str | None = None
    last_error: str | None = None


@dataclass(frozen=True)
class IngestCandidate:
    row: Any
    metadata: dict[str, Any]
    request: dict[str, Any]
    fingerprint: str
    legacy_fingerprint: str
    cached_result: dict[str, Any] | None
    input_tokens: int
    topic: str
    reason: str = "all"
    topic_count: int = 1

    @property
    def resource_id(self) -> str:
        return str(self.row["resource_id"])

    @property
    def title(self) -> str:
        return str(self.row["title"] or self.metadata.get("title") or "Untitled")

    @property
    def source_ref(self) -> str:
        provider = str(self.metadata.get("_feedian_source") or "unknown")
        native_id = str(
            self.metadata.get("_feedian_source_id") or self.metadata.get("_id") or "unknown"
        )
        safe_native_id = re.sub(r"\s+", " ", native_id).strip()[:120]
        return f"{provider}:{safe_native_id}"


@dataclass(frozen=True)
class IngestPlan:
    candidates: tuple[IngestCandidate, ...]
    total_resources: int
    new_requests: int
    reusable: int
    input_tokens: int
    estimated_output_tokens: int | None
    max_output_tokens: int
    estimated_cost_usd: float | None
    max_cost_usd: float | None
    usage_records: int = 0
    auto: bool = False


IngestProgress = Callable[[int, int, IngestCandidate, IngestReport], None]


def plan_source_notes(
    store: VaultStore,
    *,
    model: str,
    language: str = "Japanese",
    limit: int | None = None,
    force: bool = False,
    auto: bool = False,
    backend: str = "openai-responses",
    provider: str | None = None,
    backend_instance: LLMBackend | None = None,
) -> IngestPlan:
    backend_id = canonical_backend_id(provider or backend)
    selected_backend = backend_instance or get_backend(backend_id)
    rows = _source_rows(store)
    all_candidates = [
        _candidate(
            store,
            row,
            model=model,
            language=language,
            force=force,
            backend=backend_id,
            backend_instance=selected_backend,
        )
        for row in rows
    ]
    if auto:
        effective_limit = 20 if limit is None else limit
        candidates = _select_auto_candidates(store, all_candidates, effective_limit)
    else:
        candidates = all_candidates if limit is None else all_candidates[:limit]
    new_requests = sum(candidate.cached_result is None for candidate in candidates)
    reusable = len(candidates) - new_requests
    input_tokens = sum(candidate.input_tokens for candidate in candidates if candidate.cached_result is None)
    output_ratio, usage_records = _historical_output_ratio(store, backend_id, model)
    estimated_output_tokens = (
        sum(min(round(candidate.input_tokens * output_ratio), 800) for candidate in candidates if candidate.cached_result is None)
        if output_ratio is not None else None
    )
    return IngestPlan(
        candidates=tuple(candidates),
        total_resources=len(rows),
        new_requests=new_requests,
        reusable=reusable,
        input_tokens=input_tokens,
        estimated_output_tokens=estimated_output_tokens,
        max_output_tokens=new_requests * 800,
        estimated_cost_usd=(
            _maximum_cost(
                input_tokens, estimated_output_tokens, model,
                billing_mode=selected_backend.capabilities.billing_mode,
            )
            if estimated_output_tokens is not None else None
        ),
        max_cost_usd=_maximum_cost(
            input_tokens, new_requests * 800, model,
            billing_mode=selected_backend.capabilities.billing_mode,
        ),
        usage_records=usage_records,
        auto=auto,
    )


def ingest_source_notes(
    store: VaultStore,
    vault_root: str | Path,
    config: VaultConfig,
    *,
    model: str,
    language: str = "Japanese",
    limit: int | None = None,
    force: bool = False,
    auto: bool = False,
    dry_run: bool = False,
    progress: IngestProgress | None = None,
    plan: IngestPlan | None = None,
    backend: str = "openai-responses",
    provider: str | None = None,
    backend_instance: LLMBackend | None = None,
    fallback_instance: LLMBackend | None = None,
) -> IngestReport:
    backend_id = canonical_backend_id(provider or backend)
    selected_backend = backend_instance or get_backend(backend_id)
    plan = plan or plan_source_notes(
        store,
        model=model,
        language=language,
        limit=limit,
        force=force,
        auto=auto,
        backend=backend_id,
        backend_instance=selected_backend,
    )
    if dry_run:
        return IngestReport(processed=len(plan.candidates), reused=plan.reusable)
    # Past the dry-run return, so planning stays read-only: the CLI plans a dry
    # run without the vault write lock, and this writes. Runs a killed process
    # left open are not pending work, but only this process may say so - under
    # the lock, another ingest's live runs are not ours to fail.
    store.fail_interrupted_llm_runs()
    if plan.new_requests and not selected_backend.supports_model(model):
        raise BackendPolicyError(f"Backend {backend_id} does not support model {model!r}.")
    preflight_metadata = selected_backend.preflight() if plan.new_requests else {}
    temporary_parent = (
        _temporary_parent_for(selected_backend, vault_root) if plan.new_requests
        else vault_paths(vault_root).state_dir / "tmp"
    )
    fallback = (
        resolve_fallback(config, backend_id, backend_instance=fallback_instance)
        if plan.new_requests else None
    )
    workers = max(1, int(config.llm.workers))
    gates: dict[str, _BackendGate] = {}
    processed = created = reused = failed = input_tokens = output_tokens = 0
    unpriced = unmetered = 0
    cost_usd = 0.0
    queue: list[_Job] = []
    pending: dict[Any, _Job] = {}
    open_runs: dict[str, _Job] = {}
    interrupted = False

    def gate_for(backend: LLMBackend) -> _BackendGate:
        return gates.setdefault(backend.capabilities.backend, _BackendGate(backend.capabilities))

    def report(candidate: IngestCandidate, status: str, run_id: str | None, error: str | None) -> None:
        if progress is None:
            return
        progress(
            processed, len(plan.candidates), candidate,
            IngestReport(
                processed, created, reused, failed, input_tokens, output_tokens, cost_usd,
                unpriced, unmetered, last_status=status, last_run_id=run_id, last_error=error,
            ),
        )

    def account(job: _Job, attempt: _Attempt) -> None:
        nonlocal processed, created, reused, failed
        nonlocal input_tokens, output_tokens, cost_usd, unpriced, unmetered
        # Counted where the candidate is settled, not where its job is submitted.
        # Jobs are submitted up to the worker count ahead of the first result, so
        # counting at submit reports every candidate done as soon as one is. A
        # fallback does not reach here for the attempt it replaces, so one
        # candidate is still counted once.
        processed += 1
        if attempt.reused:
            reused += 1
            report(job.candidate, "reused", attempt.run_id, attempt.error_text)
            return
        if attempt.audit is not None:
            created += 1
            audit = attempt.audit
            input_tokens += _usage_count(audit.usage.get("input_tokens"))
            output_tokens += _usage_count(audit.usage.get("output_tokens"))
            if not audit.usage or not attempt.usage_available:
                unmetered += 1
            if isinstance(attempt.estimated_cost, (int, float)):
                cost_usd += float(attempt.estimated_cost)
            else:
                unpriced += 1
            report(job.candidate, "created", attempt.run_id, attempt.error_text)
            return
        failed += 1
        report(job.candidate, "failed", attempt.run_id, attempt.error_text)

    def settle(job: _Job) -> None:
        """Close one finished job, taking the fallback if the plan named one."""
        attempt = _close_run(store, vault_root, job)
        open_runs.pop(job.run_id, None)
        if (
            attempt.error is not None
            and fallback is not None
            and not job.is_fallback
            and getattr(attempt.error, "fallback_eligible", False)
        ):
            # Named in the plan before the run started; Feedian never picks a
            # destination on its own. Replanning reads the database, so it stays
            # here rather than moving into a worker.
            fallback_candidate = _candidate(
                store, job.row, model=fallback.model, language=language, force=force,
                backend=fallback.backend_id, backend_instance=fallback.backend,
            )
            if fallback_candidate.cached_result is not None:
                store.put_source_note(
                    resource_id=job.candidate.resource_id, llm_run_id=None,
                    markdown=render_source_note(
                        job.row, job.metadata, fallback_candidate.cached_result, model=fallback.model,
                    ),
                )
                account(job, _Attempt(run_id=attempt.run_id, reused=True))
                return
            queue.insert(0, _Job(
                candidate=fallback_candidate, row=job.row, metadata=job.metadata,
                backend_id=fallback.backend_id, backend=fallback.backend, model=fallback.model,
                temporary_parent=_temporary_parent_for(fallback.backend, vault_root),
                preflight_metadata=fallback.preflight(), is_fallback=True,
            ))
            return
        account(job, attempt)

    for candidate in plan.candidates:
        row = candidate.row
        if candidate.cached_result is not None:
            processed += 1
            # Planning only reads, so a result found under the version-one key is
            # rewritten here, where the vault write lock is held.
            store.promote_legacy_fingerprint(
                resource_revision_id=str(row["resource_revision_id"]), operation="source-note",
                model=model, prompt_version=PROMPT_VERSION,
                input_fingerprint=candidate.fingerprint,
                legacy_fingerprint=candidate.legacy_fingerprint,
                backend=backend_id, summary_schema_version=CANONICAL_SUMMARY_SCHEMA_VERSION,
            )
            store.put_source_note(
                resource_id=candidate.resource_id, llm_run_id=None,
                markdown=render_source_note(row, candidate.metadata, candidate.cached_result, model=model),
            )
            reused += 1
            report(candidate, "reused", None, None)
            continue
        queue.append(_Job(
            candidate=candidate, row=row, metadata=candidate.metadata,
            backend_id=backend_id, backend=selected_backend, model=model,
            temporary_parent=temporary_parent, preflight_metadata=preflight_metadata,
        ))

    if queue:
        executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="feedian-ingest")
        try:
            while queue or pending:
                submitted = False
                while queue and len(pending) < workers:
                    now = time.monotonic()
                    index = next(
                        (i for i, job in enumerate(queue) if gate_for(job.backend).due_at() <= now), None
                    )
                    if index is None:
                        break
                    job = queue.pop(index)
                    gate_for(job.backend).take()
                    _open_run(store, job)
                    open_runs[job.run_id] = job
                    pending[executor.submit(_execute_job, job, language=language)] = job
                    submitted = True
                if submitted and queue:
                    continue
                if not pending and queue:
                    # Nothing may start yet and nothing is running. Waiting on an
                    # empty set returns at once, so sleep to the earliest opening
                    # instead of spinning. No run and no Future exist yet, so a
                    # KeyboardInterrupt here costs nothing.
                    delay = min(gate_for(job.backend).due_at() for job in queue) - time.monotonic()
                    if delay > 0:
                        time.sleep(delay)
                    continue
                timeout = None
                if queue:
                    due = min(gate_for(job.backend).due_at() for job in queue)
                    timeout = max(0.0, due - time.monotonic()) if due != float("inf") else None
                done, _ = futures_wait(pending, timeout=timeout, return_when=FIRST_COMPLETED)
                for future in done:
                    job = pending.pop(future)
                    gate_for(job.backend).release()
                    settle(future.result())
        except BaseException:
            interrupted = True
            raise
        finally:
            if interrupted:
                _abandon_open_runs(store, vault_root, pending, open_runs)
            executor.shutdown(wait=True)

    return IngestReport(
        processed, created, reused, failed, input_tokens, output_tokens, cost_usd, unpriced, unmetered,
    )




@dataclass(frozen=True)
class _Attempt:
    run_id: str
    audit: Any = None
    error: Exception | None = None
    reused: bool = False
    error_text: str | None = None
    estimated_cost: Any = None
    usage_available: bool = True


@dataclass(frozen=True)
class _Fallback:
    backend_id: str
    backend: LLMBackend
    model: str
    _metadata: dict[str, Any] | None = None

    def preflight(self) -> dict[str, Any]:
        """Deferred: a fallback that never fires must not demand credentials."""
        return self.backend.preflight()


def _temporary_parent_for(backend: LLMBackend, vault_root: str | Path) -> Path:
    """Where this backend may create per-article working directories.

    A local agent treats its cwd as a project, so it runs outside the Vault; an
    HTTP backend never opens one and keeps the Vault's own temporary directory.
    """
    if backend.capabilities.execution_kind != "local-agent":
        return vault_paths(vault_root).state_dir / "tmp"
    try:
        return isolated_local_agent_parent(vault_root)
    except RuntimeError as exc:
        raise BackendPolicyError(str(exc)) from exc


def _abandon_open_runs(
    store: VaultStore,
    vault_root: str | Path,
    pending: dict[Any, _Job],
    open_runs: dict[str, _Job],
) -> None:
    """Close every run that will not produce a saved result, after an interrupt.

    A Future that cancels cleanly never reached the backend, so its run is a
    failure and nothing was billed. One that refuses to cancel is already
    RUNNING: it may still put its request on the wire, so it is drained and its
    real outcome recorded. The number that can still go out is bounded by the
    Futures running at that moment, which is the concurrency the user chose.

    Cancelling is done here rather than through shutdown(cancel_futures=True),
    which does not say which Futures it cancelled - leaving no way to know which
    runs to close.
    """
    cancelled = [future for future in list(pending) if future.cancel()]
    for future in cancelled:
        job = pending.pop(future, None)
        if job is None:
            continue
        job.error = RuntimeError("interrupted before the backend was called")
        _close_run(store, vault_root, job)
        open_runs.pop(job.run_id, None)
    for future in list(pending):
        job = pending.pop(future)
        try:
            future.result()
        except BaseException as exc:  # noqa: BLE001 - recorded, not handled
            job.error = job.error or exc
        _close_run(store, vault_root, job)
        open_runs.pop(job.run_id, None)
    # Anything still open was started but never handed to a Future, or its
    # submit raised. The main thread owns it either way.
    for job in list(open_runs.values()):
        job.error = job.error or RuntimeError("interrupted before the backend was called")
        _close_run(store, vault_root, job)
        open_runs.pop(job.run_id, None)


class _BackendGate:
    """Admission for one backend: how many may run, and how often one may start.

    Both live here rather than inside the worker. A worker that sleeps on a start
    interval is RUNNING while it has sent nothing, and Future.cancel() cannot
    reach it - so an interrupt during that sleep still lets the request go out.
    Deciding before submit keeps the wait where a KeyboardInterrupt can end it.
    """

    def __init__(self, capabilities: Any) -> None:
        self.limit = max(1, int(getattr(capabilities, "max_parallelism", 1) or 1))
        self.interval = max(0.0, float(getattr(capabilities, "min_start_interval_seconds", 0.0) or 0.0))
        self.running = 0
        self.next_start_at = 0.0

    def due_at(self) -> float:
        return self.next_start_at if self.running < self.limit else float("inf")

    def take(self) -> None:
        self.running += 1
        self.next_start_at = max(time.monotonic(), self.next_start_at) + self.interval

    def release(self) -> None:
        self.running = max(0, self.running - 1)


@dataclass
class _Job:
    """One summary from the moment its run is opened to the moment it is closed."""

    candidate: IngestCandidate
    row: Any
    metadata: dict[str, Any]
    backend_id: str
    backend: LLMBackend
    model: str
    temporary_parent: Path
    preflight_metadata: dict[str, Any]
    is_fallback: bool = False
    run_id: str = ""
    started_at: float = 0.0
    audit: Any = None
    error: Exception | None = None


def _open_run(store: VaultStore, job: _Job) -> None:
    """Start the audit row. Called immediately before the job is submitted."""
    job.run_id = store.start_llm_run(
        resource_id=job.candidate.resource_id,
        resource_revision_id=str(job.row["resource_revision_id"]),
        operation="source-note", model=job.model, prompt_version=PROMPT_VERSION,
        input_fingerprint=job.candidate.fingerprint,
        request={"logical": job.candidate.request, "actual": None},
        backend=job.backend_id,
        summary_schema_version=CANONICAL_SUMMARY_SCHEMA_VERSION,
        fingerprint_version=2,
        auth_mode=job.backend.capabilities.auth_mode,
        billing_mode=job.backend.capabilities.billing_mode,
        backend_metadata=job.preflight_metadata,
    )
    job.started_at = time.monotonic()


def _execute_job(job: _Job, *, language: str) -> _Job:
    """The only part that runs off the main thread: the provider call itself."""
    try:
        job.audit = job.backend.summarize(
            model=job.model, item=job.metadata, page=_page(job.row, job.metadata),
            language=language, timeout_seconds=60, max_output_tokens=800,
            reasoning_effort="low", max_retries=3, retry_base_seconds=1.0,
            temporary_parent=job.temporary_parent,
        )
    except Exception as exc:
        job.error = exc
    return job


def _close_run(store: VaultStore, vault_root: str | Path, job: _Job) -> _Attempt:
    """Write the outcome of one job. Main thread only."""
    duration_ms = round((time.monotonic() - job.started_at) * 1000)
    if job.error is not None:
        prompt = str(job.candidate.request["input"][0]["content"][0]["text"])
        error_text = sanitize_error(
            str(job.error), Path(vault_root).resolve(),
            private_values=(prompt, str(job.row["content_markdown"] or "")),
        )
        store.finish_llm_run(
            job.run_id,
            request={"logical": job.candidate.request, "actual": getattr(job.error, "request", None)},
            error=error_text,
            duration_ms=duration_ms,
        )
        return _Attempt(run_id=job.run_id, error=job.error, error_text=error_text)
    audit = job.audit
    price = _price_record(audit.usage, job.model, billing_mode=audit.billing_mode)
    store.finish_llm_run(
        job.run_id,
        request={"logical": job.candidate.request, "actual": audit.request},
        response=audit.response, result=audit.result, usage=audit.usage or None, price=price,
        auth_mode=audit.auth_mode, billing_mode=audit.billing_mode,
        backend_metadata=audit.metadata,
        duration_ms=duration_ms,
    )
    store.put_source_note(
        resource_id=job.candidate.resource_id, llm_run_id=job.run_id,
        markdown=render_source_note(job.row, job.metadata, audit.result, model=job.model),
    )
    return _Attempt(
        run_id=job.run_id, audit=audit,
        estimated_cost=price.get("estimated_cost_usd"),
        usage_available=job.backend.capabilities.usage_available,
    )



def fallback_maximum_cost(plan: IngestPlan, fallback: "_Fallback | None") -> float | None:
    """What the fallback could add if every new request failed over to it."""
    if fallback is None or not plan.new_requests:
        return None
    return _maximum_cost(
        plan.input_tokens, plan.new_requests * 800, fallback.model,
        billing_mode=fallback.backend.capabilities.billing_mode,
    )


def resolve_fallback(
    config: VaultConfig, backend_id: str, *, backend_instance: LLMBackend | None = None,
) -> _Fallback | None:
    """The configured fallback, or None. Disabled by default and never inferred."""
    settings = config.llm.fallback
    if not settings.enabled:
        return None
    destination = canonical_backend_id(settings.backend)
    if destination == backend_id:
        return None
    backend = backend_instance or get_backend(destination)
    if not backend.supports_model(settings.model):
        raise BackendPolicyError(
            f"Fallback backend {destination} does not support model {settings.model!r}."
        )
    return _Fallback(destination, backend, settings.model)


def _source_rows(store: VaultStore) -> list[Any]:
    return store.connection.execute(
        """
        SELECT r.resource_id, r.current_revision_id AS resource_revision_id,
               rr.title, rr.content_markdown, rr.discussion_text,
               sr.metadata_json
        FROM resource AS r
        JOIN resource_revision AS rr ON rr.resource_revision_id = r.current_revision_id
        JOIN source_item AS s ON s.resource_id = r.resource_id
        JOIN source_item_revision AS sr ON sr.source_revision_id = s.current_revision_id
        WHERE r.removed_at IS NULL
        GROUP BY r.resource_id
        ORDER BY rr.created_at ASC
        """
    ).fetchall()


def _candidate(
    store: VaultStore,
    row: Any,
    *,
    model: str,
    language: str,
    force: bool,
    backend: str = "openai-responses",
    backend_instance: LLMBackend | None = None,
) -> IngestCandidate:
    backend_id = canonical_backend_id(backend)
    selected_backend = backend_instance or get_backend(backend_id)
    metadata = json.loads(str(row["metadata_json"]))
    request = build_summary_request(
        model=model, item=metadata, page=_page(row, metadata), language=language,
        max_output_tokens=800, reasoning_effort="low",
        max_article_chars=selected_backend.capabilities.max_article_chars,
    )
    fingerprint = hashlib.sha256(stable_json(request).encode("utf-8")).hexdigest()
    # Rebuild the key exactly as the release before backend IDs wrote it, from a
    # frozen schema, so editing the provider schema cannot silently end the
    # migration window. A prompt change is meant to invalidate reuse and does so
    # through PROMPT_VERSION.
    legacy_request = deepcopy(request)
    legacy_request["text"]["format"]["schema"] = LEGACY_V1_PROVIDER_SCHEMA
    legacy_request["provider"] = "manus" if backend_id == "manus-api" else "openai"
    legacy_fingerprint = hashlib.sha256(stable_json(legacy_request).encode("utf-8")).hexdigest()
    cached = None if force else store.successful_llm_result(
        resource_revision_id=str(row["resource_revision_id"]), operation="source-note", model=model,
        prompt_version=PROMPT_VERSION, input_fingerprint=fingerprint, backend=backend_id,
        summary_schema_version=CANONICAL_SUMMARY_SCHEMA_VERSION,
        legacy_fingerprint=legacy_fingerprint,
    )
    prompt = str(request["input"][0]["content"][0]["text"])
    input_tokens, _ = count_prompt_tokens(f"{SUMMARY_INSTRUCTIONS}\n\n{prompt}", model)
    return IngestCandidate(
        row, metadata, request, fingerprint, legacy_fingerprint, cached, input_tokens, _topics(metadata)[0]
    )


def _page(row: Any, metadata: dict[str, Any]) -> PageFetchResult:
    return PageFetchResult(
        url=str(metadata.get("link") or ""), text=str(row["content_markdown"] or ""),
        title=str(row["title"] or metadata.get("title") or ""),
        discussion_text=str(row["discussion_text"] or ""),
    )


def _select_auto_candidates(
    store: VaultStore, candidates: list[IngestCandidate], limit: int,
) -> list[IngestCandidate]:
    if limit <= 0:
        return []
    covered_ids = {
        str(row[0]) for row in store.connection.execute(
            "SELECT resource_id FROM source_note WHERE superseded_at IS NULL"
        )
    }
    covered_topics = {
        topic
        for candidate in candidates if candidate.resource_id in covered_ids
        for topic in _topics(candidate.metadata)
    }
    actionable = [
        candidate for candidate in candidates if candidate.resource_id not in covered_ids
    ]
    topic_counts: dict[str, int] = {}
    buckets: dict[str, list[IngestCandidate]] = {}
    for candidate in actionable:
        for topic in _topics(candidate.metadata):
            topic_counts[topic] = topic_counts.get(topic, 0) + 1
    for candidate in actionable:
        topics = _topics(candidate.metadata)
        topic = sorted(
            topics,
            key=lambda value: (-topic_counts[value], value in covered_topics, value),
        )[0]
        reason = "uncovered-field" if topic not in covered_topics else "largest-field"
        selected = IngestCandidate(
            candidate.row, candidate.metadata, candidate.request, candidate.fingerprint,
            candidate.legacy_fingerprint, candidate.cached_result, candidate.input_tokens, topic,
            reason=reason, topic_count=topic_counts[topic],
        )
        buckets.setdefault(topic, []).append(selected)
    for bucket in buckets.values():
        bucket.sort(key=lambda value: (-len(str(value.row["content_markdown"] or "")), value.title))
    largest_topics = sorted(
        buckets, key=lambda topic: (-topic_counts[topic], topic in covered_topics, topic),
    )
    uncovered_topics = [topic for topic in largest_topics if topic not in covered_topics]
    ordered_topics: list[str] = []
    for index in range(max(len(largest_topics), len(uncovered_topics))):
        for topics in (largest_topics, uncovered_topics):
            if index < len(topics) and topics[index] not in ordered_topics:
                ordered_topics.append(topics[index])
    selected: list[IngestCandidate] = []
    while len(selected) < limit:
        added = False
        for topic in ordered_topics:
            if buckets[topic]:
                selected.append(buckets[topic].pop(0))
                added = True
                if len(selected) == limit:
                    break
        if not added:
            break
    return selected


_GENERIC_TOPICS = frozenset({"article", "bookmark", "news", "web", "あとで読む", "未分類"})


def _topics(metadata: dict[str, Any]) -> list[str]:
    topics: list[str] = []
    for value in metadata.get("tags") or []:
        topic = re.sub(r"\s+", "-", str(value).strip().lower().lstrip("#"))
        if topic and topic not in _GENERIC_TOPICS and topic not in topics:
            topics.append(topic)
    if topics:
        return topics
    title = str(metadata.get("title") or "")
    for value in re.findall(r"[A-Za-z][A-Za-z0-9.+#-]{2,}|[一-龯ァ-ヶー]{2,12}", title):
        topic = value.lower()
        if topic not in _GENERIC_TOPICS and topic not in topics:
            topics.append(topic)
        if len(topics) == 3:
            break
    if topics:
        return topics
    hostname = (urlsplit(str(metadata.get("link") or "")).hostname or "unknown").lower()
    return [hostname.removeprefix("www.")]


def _maximum_cost(
    input_tokens: int, output_tokens: int, model: str, *, billing_mode: str = "metered-api"
) -> float | None:
    if billing_mode != "metered-api":
        return None
    pricing_model = comparison_model(model)
    price = next((value for value in MODEL_PRICES if value.model == pricing_model), None)
    if price is None:
        return None
    return (input_tokens * price.input_per_million + output_tokens * price.output_per_million) / 1_000_000


def _historical_output_ratio(
    store: VaultStore, backend: str, model: str
) -> tuple[float | None, int]:
    rows = store.connection.execute(
        """
        SELECT usage_json FROM llm_run
        WHERE operation = 'source-note' AND backend = ? AND model = ? AND status = 'completed'
          AND usage_json IS NOT NULL
        """,
        (backend, model),
    ).fetchall()
    input_total = output_total = records = 0
    for row in rows:
        try:
            usage = json.loads(str(row["usage_json"]))
        except (TypeError, json.JSONDecodeError):
            continue
        input_tokens = _usage_count(usage.get("input_tokens"))
        output_tokens = _usage_count(usage.get("output_tokens"))
        if input_tokens <= 0:
            continue
        input_total += input_tokens
        output_total += output_tokens
        records += 1
    return ((output_total / input_total) if input_total else None, records)


def _usage_count(value: Any) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def render_source_notes(store: VaultStore, vault_root: str | Path, config: VaultConfig) -> tuple[int, int]:
    root = Path(vault_root).resolve()
    output = root / config.source_folder
    rows = store.connection.execute(
        """
        SELECT sn.source_note_id, sn.resource_id, sn.markdown, rr.title
        FROM source_note AS sn
        JOIN resource AS r ON r.resource_id = sn.resource_id
        LEFT JOIN resource_revision AS rr ON rr.resource_revision_id = r.current_revision_id
        WHERE sn.superseded_at IS NULL
        ORDER BY sn.created_at
        """
    ).fetchall()
    written = skipped = 0
    for row in rows:
        title = sanitize_filename(str(row["title"] or "Untitled"))[:60].rstrip(" .") or "Untitled"
        path = output / f"{title} - {str(row['resource_id'])[:8]}.md"
        document = str(row["markdown"])
        if path.exists() and path.read_text(encoding="utf-8") == document:
            skipped += 1
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(document, encoding="utf-8", newline="\n")
        written += 1
    return written, skipped


def render_source_note(row: Any, metadata: dict[str, Any], result: dict[str, Any], *, model: str) -> str:
    title = str(result.get("note_title") or row["title"] or metadata.get("title") or "Untitled")
    frontmatter = {
        "feedian_managed": True,
        "feedian_kind": "source",
        "resource_id": row["resource_id"],
        "source": metadata.get("link") or "",
        "title": title,
        "model": model,
        "tags": [str(tag) for tag in result.get("tags") or []],
    }
    lines = ["---", yaml_frontmatter(frontmatter), "---", "", f"# {escape_markdown_heading(title)}", ""]
    lines.extend(["## Summary", "", str(result.get("summary") or "").strip(), ""])
    points = [str(point).strip() for point in result.get("key_points") or [] if str(point).strip()]
    if points:
        lines.extend(["## Key Points", "", *[f"- {point}" for point in points], ""])
    tags = [str(tag).strip() for tag in result.get("tags") or [] if str(tag).strip()]
    if tags:
        lines.extend(["## Tags", "", " ".join(f"#{tag}" for tag in tags), ""])
    lines.extend(["## Source", "", f"- {metadata.get('link') or ''}", f"- Content type: {result.get('content_type') or ''}", ""])
    return "\n".join(lines).rstrip() + "\n"


def _price_record(
    usage: dict[str, Any], model: str, *, billing_mode: str = "metered-api"
) -> dict[str, Any]:
    if billing_mode != "metered-api":
        return {
            "model": model,
            "billing_mode": billing_mode,
            "source": "not-metered-api",
            "estimated_cost_usd": None,
        }
    pricing_model = comparison_model(model)
    price = next((value for value in MODEL_PRICES if value.model == pricing_model), None)
    if price is None:
        return {"model": model, "source": "unavailable", "estimated_cost_usd": None}
    return {
        "model": model,
        "source": "built-in",
        "input_per_million": price.input_per_million,
        "cached_input_per_million": price.cached_input_per_million,
        "output_per_million": price.output_per_million,
        "estimated_cost_usd": usage_cost_usd(usage, price),
    }
