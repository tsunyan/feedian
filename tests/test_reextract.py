from __future__ import annotations

from io import BytesIO

from pypdf import PdfWriter

from feedian.canonical import CanonicalItem
from feedian.reextract import reextract_stored_resources
from feedian.store import VaultStore


def test_reextract_uses_stored_pdf_without_network(tmp_path) -> None:
    output = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(output)
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(
            CanonicalItem(source="hatena", source_id="pdf", content_key="url:pdf", url="https://example.test/a.pdf")
        )
        payload = store.put_payload(output.getvalue(), media_type="application/pdf", source_url="https://example.test/a.pdf")
        store.record_resource_revision(
            item.resource_id or "", content_markdown="", final_url="https://example.test/a.pdf",
            extracted_by="http", http_payload_id=payload, warning="unsupported content type: application/pdf",
        )

        report = reextract_stored_resources(store, media_type="application/pdf")

        assert report.processed == 1
        assert report.failed == 0
        warning = store.connection.execute(
            "SELECT warning FROM fetch_capture ORDER BY fetched_at DESC LIMIT 1"
        ).fetchone()[0]
        assert "OCR" in warning
    finally:
        store.close()


def test_reextract_splits_failed_and_successful_extractions_between_resources(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        failing_item = store.upsert_canonical_item(
            CanonicalItem(source="hatena", source_id="broken", content_key="url:broken", url="https://example.test/broken")
        )
        failing_payload = store.put_payload(
            b"binary junk", media_type="application/octet-stream", source_url="https://example.test/broken"
        )
        # Set up the fixture the way sync.py's failed-fetch split does: a fetch_capture
        # row carrying the payload, with no resource_revision and current_revision_id
        # left NULL, so the "still failing on re-extraction" branch is exercised from a
        # clean starting point rather than one that already has an (empty) revision.
        store.record_failed_fetch(
            failing_item.resource_id or "", warning="initial fetch failed", http_payload_id=failing_payload,
            final_url="https://example.test/broken",
        )

        succeeding_item = store.upsert_canonical_item(
            CanonicalItem(source="hatena", source_id="ok", content_key="url:ok", url="https://example.test/ok")
        )
        succeeding_payload = store.put_payload(
            b"Real article body text", media_type="text/plain", source_url="https://example.test/ok"
        )
        store.record_failed_fetch(
            succeeding_item.resource_id or "", warning="initial fetch failed", http_payload_id=succeeding_payload,
            final_url="https://example.test/ok",
        )

        report = reextract_stored_resources(store)

        assert report.processed == 2
        assert report.failed == 0
        assert report.changed == 1

        failing_resource = store.connection.execute(
            "SELECT current_revision_id FROM resource WHERE resource_id = ?", (failing_item.resource_id,)
        ).fetchone()
        assert failing_resource["current_revision_id"] is None
        assert store.connection.execute(
            "SELECT COUNT(*) FROM resource_revision WHERE resource_id = ?", (failing_item.resource_id,)
        ).fetchone()[0] == 0

        succeeding_resource = store.connection.execute(
            "SELECT current_revision_id FROM resource WHERE resource_id = ?", (succeeding_item.resource_id,)
        ).fetchone()
        assert succeeding_resource["current_revision_id"] is not None
        content = store.connection.execute(
            "SELECT content_markdown FROM resource_revision WHERE resource_id = ?", (succeeding_item.resource_id,)
        ).fetchone()[0]
        assert content == "Real article body text"
    finally:
        store.close()
