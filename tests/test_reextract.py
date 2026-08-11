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
