from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .estimate import MODEL_PRICES, comparison_model, count_prompt_tokens, usage_cost_usd
from .extract import PageFetchResult
from .llm import SUMMARY_INSTRUCTIONS, build_summary_request, summarize_bookmark_with_audit
from .markdown import escape_markdown_heading, sanitize_filename, yaml_frontmatter
from .store import VaultStore, stable_json
from .vault import VaultConfig


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
    provider: str = "openai",
) -> IngestPlan:
    rows = _source_rows(store)
    all_candidates = [
        _candidate(store, row, model=model, language=language, force=force, provider=provider)
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
    output_ratio, usage_records = _historical_output_ratio(store, model)
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
            _maximum_cost(input_tokens, estimated_output_tokens, model)
            if estimated_output_tokens is not None else None
        ),
        max_cost_usd=_maximum_cost(input_tokens, new_requests * 800, model),
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
    provider: str = "openai",
) -> IngestReport:
    plan = plan or plan_source_notes(
        store, model=model, language=language, limit=limit, force=force, auto=auto, provider=provider,
    )
    if dry_run:
        return IngestReport(processed=len(plan.candidates), reused=plan.reusable)
    api_key_name = "MANUS_API_KEY" if provider == "manus" else "OPENAI_API_KEY"
    api_key = os.environ.get(api_key_name, "").strip()
    if plan.new_requests and not api_key:
        raise RuntimeError(f"Missing required environment variable: {api_key_name}")
    processed = created = reused = failed = input_tokens = output_tokens = 0
    unpriced = unmetered = 0
    cost_usd = 0.0
    max_article_chars = 3_000 if provider == "manus" else 10_000
    for candidate in plan.candidates:
        row = candidate.row
        metadata = candidate.metadata
        processed += 1
        if candidate.cached_result is not None:
            store.put_source_note(
                resource_id=candidate.resource_id, llm_run_id=None,
                markdown=render_source_note(row, metadata, candidate.cached_result, model=model),
            )
            reused += 1
            if progress is not None:
                progress(
                    processed, len(plan.candidates), candidate,
                    IngestReport(
                        processed, created, reused, failed, input_tokens, output_tokens, cost_usd,
                        unpriced, unmetered, last_status="reused",
                    ),
                )
            continue
        run_id = store.start_llm_run(
            resource_id=candidate.resource_id, resource_revision_id=str(row["resource_revision_id"]),
            operation="source-note", model=model, prompt_version=PROMPT_VERSION,
            input_fingerprint=candidate.fingerprint, request=candidate.request,
        )
        item_status = "created"
        item_error = None
        try:
            page = _page(row, metadata)
            audit = summarize_bookmark_with_audit(
                api_key, model, metadata, page, language, 60, 800, "low", 3, 1.0, max_article_chars,
                provider=provider,
            )
            price = _price_record(audit.usage, model)
            store.finish_llm_run(
                run_id, request=audit.request, response=audit.response,
                result=audit.result, usage=audit.usage, price=price,
            )
            store.put_source_note(
                resource_id=candidate.resource_id, llm_run_id=run_id,
                markdown=render_source_note(row, metadata, audit.result, model=model),
            )
            created += 1
            input_tokens += _usage_count(audit.usage.get("input_tokens"))
            output_tokens += _usage_count(audit.usage.get("output_tokens"))
            if not audit.usage:
                unmetered += 1
            estimated_cost = price.get("estimated_cost_usd")
            if isinstance(estimated_cost, (int, float)):
                cost_usd += float(estimated_cost)
            else:
                unpriced += 1
        except Exception as exc:
            item_status = "failed"
            item_error = str(exc)
            store.finish_llm_run(run_id, error=item_error)
            failed += 1
        if progress is not None:
            progress(
                processed, len(plan.candidates), candidate,
                IngestReport(
                    processed, created, reused, failed, input_tokens, output_tokens, cost_usd,
                    unpriced, unmetered,
                    last_status=item_status, last_run_id=run_id, last_error=item_error,
                ),
            )
    return IngestReport(
        processed, created, reused, failed, input_tokens, output_tokens, cost_usd, unpriced, unmetered,
    )


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
    store: VaultStore, row: Any, *, model: str, language: str, force: bool, provider: str = "openai",
) -> IngestCandidate:
    metadata = json.loads(str(row["metadata_json"]))
    max_article_chars = 3_000 if provider == "manus" else 10_000
    request = build_summary_request(
        model=model, item=metadata, page=_page(row, metadata), language=language,
        max_output_tokens=800, reasoning_effort="low", max_article_chars=max_article_chars,
    )
    request["provider"] = provider
    fingerprint = hashlib.sha256(stable_json(request).encode("utf-8")).hexdigest()
    cached = None if force else store.successful_llm_result(
        resource_revision_id=str(row["resource_revision_id"]), operation="source-note", model=model,
        prompt_version=PROMPT_VERSION, input_fingerprint=fingerprint,
    )
    prompt = str(request["input"][0]["content"][0]["text"])
    input_tokens, _ = count_prompt_tokens(f"{SUMMARY_INSTRUCTIONS}\n\n{prompt}", model)
    return IngestCandidate(row, metadata, request, fingerprint, cached, input_tokens, _topics(metadata)[0])


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
            candidate.cached_result, candidate.input_tokens, topic,
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


def _maximum_cost(input_tokens: int, output_tokens: int, model: str) -> float | None:
    pricing_model = comparison_model(model)
    price = next((value for value in MODEL_PRICES if value.model == pricing_model), None)
    if price is None:
        return None
    return (input_tokens * price.input_per_million + output_tokens * price.output_per_million) / 1_000_000


def _historical_output_ratio(store: VaultStore, model: str) -> tuple[float | None, int]:
    rows = store.connection.execute(
        """
        SELECT usage_json FROM llm_run
        WHERE operation = 'source-note' AND model = ? AND status = 'completed'
          AND usage_json IS NOT NULL
        """,
        (model,),
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


def _price_record(usage: dict[str, Any], model: str) -> dict[str, Any]:
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
