from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import feedian.image_ocr as image_ocr_module
from feedian.canonical import CanonicalItem
from feedian.cli import build_parser
from feedian.image_ocr import (
    ImageFetchResult,
    enrich_images,
    extract_svg_text,
    prefetch_ignored_reason,
    raster_dimensions,
    _due,
)
from feedian.ingest import render_source_notes
from feedian.llm_backends import BackendAudit, BackendCapabilities
from feedian.store import VaultStore
from feedian.vault import ImageOCRSettings, VaultConfig


class FakeImageBackend:
    def __init__(self) -> None:
        self.calls = 0
        self.capabilities = BackendCapabilities(
            backend="openai-responses", execution_kind="http", auth_mode="api-key",
            billing_mode="metered-api", max_article_chars=10_000, usage_available=True,
            image_analysis=True, max_parallelism=8,
        )

    def preflight(self):
        return {"implementation_revision": "test"}

    def analyze_image(self, **kwargs):
        self.calls += 1
        assert Path(kwargs["image_path"]).is_file()
        return BackendAudit(
            result={"image_kind": "explanatory", "ocr_text": "Figure text", "ocr_truncated": False},
            request={"mode": "test"}, response={"ok": True},
            usage={"input_tokens": 10, "output_tokens": 3}, auth_mode="api-key",
            billing_mode="metered-api", metadata={"implementation_revision": "test"},
        )


def _seed_resource(store: VaultStore, source_id: str, *, image_url: str, alt: str = "Diagram") -> tuple[str, str]:
    item = store.upsert_canonical_item(CanonicalItem(
        source="hatena", source_id=source_id, content_key=f"url:{source_id}",
        url=f"https://example.test/{source_id}", title="Same title",
        created_at=f"2026-08-{10 + int(source_id)}T00:00:00+00:00",
    ))
    resource_id = item.resource_id or ""
    revision_id, _ = store.record_resource_revision(
        resource_id, content_markdown="Body", title="Same title",
    )
    store.replace_resource_images(
        resource_id=resource_id, resource_revision_id=revision_id, images=[(image_url, alt)],
    )
    return resource_id, revision_id


def test_prefetch_gate_is_strict_and_identifies_the_rule() -> None:
    assert prefetch_ignored_reason("https://example.test/assets/site-logo.png") == "name_pattern:logo"
    assert prefetch_ignored_reason("https://pbs.twimg.com/media/ABC.jpg") == "denylist:pbs.twimg.com/media"
    assert prefetch_ignored_reason("https://example.test/figures/chart.png") is None


def test_raster_header_dimensions_and_animation() -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + (640).to_bytes(4, "big") + (480).to_bytes(4, "big")
    assert raster_dimensions("image/png", png) == (640, 480, False)
    assert raster_dimensions("image/png", png + b"acTL") == (640, 480, True)


def test_svg_text_is_extracted_safely_and_limited() -> None:
    settings = ImageOCRSettings(max_ocr_chars_per_image=5)
    result = extract_svg_text(
        b'<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300"><text>Hello world</text></svg>',
        settings,
    )
    assert result.status == "completed"
    assert result.method == "svg_text"
    assert result.ocr_text == "Hello"
    assert result.ocr_truncated is True

    unsafe = extract_svg_text(
        b'<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><svg width="400" height="300"/>',
        settings,
    )
    assert unsafe.status == "failed"
    assert unsafe.failure_kind == "unsafe_svg"


def test_enrichment_shares_fetch_and_analysis_and_propagates_outside_limit(tmp_path, monkeypatch) -> None:
    store = VaultStore.open(tmp_path / ".feedian" / "feedian.sqlite3")
    image_url = "https://images.example.test/chart.png"
    first_id, _ = _seed_resource(store, "1", image_url=image_url)
    second_id, _ = _seed_resource(store, "2", image_url=image_url)
    content = b"image bytes"
    fetch_calls: list[str] = []

    def fake_fetch(url, config, temporary_parent):
        del config
        fetch_calls.append(url)
        path = temporary_parent / "fake.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return ImageFetchResult(
            status="ready", source_url=url, media_type="image/png", temporary_path=path,
            image_sha256=hashlib.sha256(content).hexdigest(), width=640, height=480,
        )

    monkeypatch.setattr(image_ocr_module, "fetch_image", fake_fetch)
    backend = FakeImageBackend()
    config = VaultConfig()
    try:
        report = enrich_images(
            store, tmp_path, config, limit=1, all_resources=False,
            backend_instance=backend,
        )
        rows = store.connection.execute(
            "SELECT resource_id, analysis_status, ocr_text FROM resource_image ORDER BY resource_id"
        ).fetchall()
    finally:
        store.close()

    assert fetch_calls == [image_url]
    assert backend.calls == 1
    assert report.resources == 1
    assert report.propagated_rows == 2
    assert not (tmp_path / ".feedian" / "tmp" / "fake.png").exists()
    assert {row["resource_id"] for row in rows} == {first_id, second_id}
    assert all(row["analysis_status"] == "completed" and row["ocr_text"] == "Figure text" for row in rows)


def test_source_render_uses_full_uuid_and_only_removes_safe_old_file(tmp_path) -> None:
    store = VaultStore.open(tmp_path / ".feedian" / "feedian.sqlite3")
    resource_id, _ = _seed_resource(store, "1", image_url="https://example.test/chart.png")
    document = (
        "---\nfeedian_managed: true\nfeedian_kind: \"source\"\n"
        f"resource_id: \"{resource_id}\"\n---\n\n# Source\n"
    )
    store.put_source_note(resource_id=resource_id, llm_run_id=None, markdown=document)
    source = tmp_path / "source"
    source.mkdir()
    old = source / f"Same title - {resource_id[:8]}.md"
    old.write_text(document, encoding="utf-8")
    try:
        report = render_source_notes(store, tmp_path, VaultConfig())
        canonical = source / f"Same title - {resource_id}.md"
        assert canonical.read_text(encoding="utf-8") == document
        assert not old.exists()
        assert report.written == 1
        assert report.migrated == 1

        protected = source / "edited old source.md"
        protected.write_text(document + "Human edit\n", encoding="utf-8")
        second = render_source_notes(store, tmp_path, VaultConfig())
        assert protected.exists()
        assert second.protected == 1
    finally:
        store.close()


def test_source_render_blocks_nonmanaged_canonical_collision(tmp_path) -> None:
    store = VaultStore.open(tmp_path / ".feedian" / "feedian.sqlite3")
    resource_id, _ = _seed_resource(store, "1", image_url="https://example.test/chart.png")
    document = (
        "---\nfeedian_managed: true\nfeedian_kind: \"source\"\n"
        f"resource_id: \"{resource_id}\"\n---\n"
    )
    store.put_source_note(resource_id=resource_id, llm_run_id=None, markdown=document)
    canonical = tmp_path / "source" / f"Same title - {resource_id}.md"
    canonical.parent.mkdir()
    canonical.write_text("human file", encoding="utf-8")
    try:
        report = render_source_notes(store, tmp_path, VaultConfig())
    finally:
        store.close()
    assert report.blocking_conflicts == 1
    assert canonical.read_text(encoding="utf-8") == "human file"


def test_enrich_images_cli_requires_limit_or_all() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["enrich-images"])


def test_transient_failure_gets_one_retry_even_when_old_ocr_is_retained() -> None:
    settings = ImageOCRSettings()
    row = {
        "last_attempt_target": "target", "last_attempt_status": "failed",
        "last_failure_kind": "transient:timeout", "transient_retry_used": 0,
        "ocr_truncated": 0, "ocr_char_limit": 2_000, "analysis_status": "completed",
    }
    assert _due(row, "target", settings, False) is True
    row["transient_retry_used"] = 1
    assert _due(row, "target", settings, False) is False
    row["last_failure_kind"] = "http_404"
    row["transient_retry_used"] = 0
    assert _due(row, "target", settings, False) is False
