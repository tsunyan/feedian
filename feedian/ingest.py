from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .estimate import MODEL_PRICES, comparison_model, usage_cost_usd
from .extract import PageFetchResult
from .llm import build_summary_request, summarize_bookmark_with_audit
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


def ingest_source_notes(
    store: VaultStore,
    vault_root: str | Path,
    config: VaultConfig,
    *,
    model: str,
    language: str = "Japanese",
    limit: int | None = None,
    force: bool = False,
) -> IngestReport:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Missing required environment variable: OPENAI_API_KEY")
    rows = store.connection.execute(
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
    if limit is not None:
        rows = rows[:limit]
    processed = created = reused = failed = 0
    for row in rows:
        processed += 1
        metadata = json.loads(str(row["metadata_json"]))
        page = PageFetchResult(
            url=str(metadata.get("link") or ""), text=str(row["content_markdown"] or ""),
            title=str(row["title"] or metadata.get("title") or ""), discussion_text=str(row["discussion_text"] or ""),
        )
        request = build_summary_request(
            model=model, item=metadata, page=page, language=language,
            max_output_tokens=800, reasoning_effort="low", max_article_chars=10_000,
        )
        fingerprint = hashlib.sha256(stable_json(request).encode("utf-8")).hexdigest()
        cached = None if force else store.successful_llm_result(
            resource_revision_id=str(row["resource_revision_id"]), operation="source-note", model=model,
            prompt_version=PROMPT_VERSION, input_fingerprint=fingerprint,
        )
        if cached is not None:
            store.put_source_note(
                resource_id=str(row["resource_id"]), llm_run_id=None,
                markdown=render_source_note(row, metadata, cached, model=model),
            )
            reused += 1
            continue
        run_id = store.start_llm_run(
            resource_id=str(row["resource_id"]), resource_revision_id=str(row["resource_revision_id"]), operation="source-note",
            model=model, prompt_version=PROMPT_VERSION, input_fingerprint=fingerprint, request=request,
        )
        try:
            audit = summarize_bookmark_with_audit(
                api_key, model, metadata, page, language, 60, 800, "low", 3, 1.0, 10_000,
            )
            price = _price_record(audit.usage, model)
            store.finish_llm_run(run_id, response=audit.response, result=audit.result, usage=audit.usage, price=price)
            store.put_source_note(
                resource_id=str(row["resource_id"]), llm_run_id=run_id,
                markdown=render_source_note(row, metadata, audit.result, model=model),
            )
            created += 1
        except Exception as exc:
            store.finish_llm_run(run_id, error=str(exc))
            failed += 1
    return IngestReport(processed, created, reused, failed)


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
