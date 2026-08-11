from __future__ import annotations

from dataclasses import dataclass

from .extract import extract_stored_payload
from .store import VaultStore


@dataclass(frozen=True)
class ReextractReport:
    processed: int = 0
    changed: int = 0
    failed: int = 0


def reextract_stored_resources(
    store: VaultStore, *, media_type: str | None = None, limit: int | None = None
) -> ReextractReport:
    parameters: list[object] = []
    media_filter = ""
    if media_type:
        media_filter = "AND lower(p.media_type) LIKE ?"
        parameters.append(media_type.lower().rstrip("%") + "%")
    rows = store.connection.execute(
        f"""
        SELECT fc.resource_id, fc.http_payload_id, fc.final_url, p.content, p.media_type
        FROM fetch_capture AS fc
        JOIN payload AS p ON p.payload_id = fc.http_payload_id
        WHERE fc.fetch_capture_id = (
            SELECT newest.fetch_capture_id FROM fetch_capture AS newest
            WHERE newest.resource_id = fc.resource_id AND newest.http_payload_id IS NOT NULL
            ORDER BY newest.fetched_at DESC LIMIT 1
        )
        {media_filter}
        ORDER BY fc.fetched_at
        """,
        parameters,
    ).fetchall()
    if limit is not None:
        rows = rows[:limit]
    processed = changed = failed = 0
    for row in rows:
        processed += 1
        try:
            result = extract_stored_payload(bytes(row["content"]), str(row["final_url"]), str(row["media_type"]))
            _, revision_changed = store.record_resource_revision(
                str(row["resource_id"]), content_markdown=result.text, title=result.title,
                final_url=result.final_url, extracted_by=f"stored:{result.extraction_method}",
                http_payload_id=str(row["http_payload_id"]), discussion_text=result.discussion_text,
                warning=result.error,
            )
            changed += int(revision_changed)
        except Exception:
            failed += 1
    return ReextractReport(processed, changed, failed)
