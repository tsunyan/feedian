from __future__ import annotations

from feedian.canonical import CanonicalItem
from feedian.store import VaultStore


def _item(*, title: str = "Title", comment: str = "", url: str = "https://example.test/article") -> CanonicalItem:
    return CanonicalItem(
        source="hatena",
        source_id="hatena-1",
        content_key="url:example",
        url=url,
        title=title,
        comment=comment,
        tags=["tag"],
        created_at="2026-08-11T00:00:00+00:00",
    )


def test_store_creates_schema_and_deduplicates_payload(tmp_path) -> None:
    store = VaultStore.open(tmp_path / ".feedian" / "feedian.sqlite3")
    try:
        first = store.put_payload(b"<html>same</html>", media_type="text/html")
        second = store.put_payload(b"<html>same</html>", media_type="text/html")

        assert first == second
        assert store.schema_version() == 1
        assert store.quick_check() == "ok"
        assert store.integrity_check() == "ok"
    finally:
        store.close()


def test_store_keeps_source_items_separate_and_shares_resource_by_url(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        first = store.upsert_canonical_item(_item(), source_payload=b'{"id": 1}')
        second = store.upsert_canonical_item(
            CanonicalItem(
                source="raindrop",
                source_id="raindrop-1",
                content_key="url:example",
                url="https://example.test/article#fragment",
                title="Title",
            ),
            source_payload=b'{"_id": 1}',
        )

        assert first.source_item_id != second.source_item_id
        assert first.resource_id == second.resource_id
        assert store.status_counts()["source_item"] == 2
        assert store.status_counts()["resource"] == 1
    finally:
        store.close()


def test_store_versions_metadata_only_when_it_changes(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        first = store.upsert_canonical_item(_item(), source_payload=b'{"id": 1}')
        same = store.upsert_canonical_item(_item(), source_payload=b'{"id": 1}')
        changed = store.upsert_canonical_item(_item(title="Changed"), source_payload=b'{"id": 1, "title": "Changed"}')

        assert first.changed is True
        assert same.changed is False
        assert changed.changed is True
        assert first.source_revision_id == same.source_revision_id
        assert changed.source_revision_id != first.source_revision_id
        assert store.status_counts()["source_item_revision"] == 2
    finally:
        store.close()


def test_store_versions_content_and_keeps_each_fetch_payload(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        stored = store.upsert_canonical_item(_item())
        first_payload = store.put_payload(b"<html>first</html>", media_type="text/html")
        revision, changed = store.record_resource_revision(
            stored.resource_id or "",
            content_markdown="First content",
            title="Title",
            http_payload_id=first_payload,
        )
        second_payload = store.put_payload(b"<html>different wrapper</html>", media_type="text/html")
        same_revision, changed_again = store.record_resource_revision(
            stored.resource_id or "",
            content_markdown="First content",
            title="Title",
            http_payload_id=second_payload,
        )

        assert changed is True
        assert changed_again is False
        assert revision == same_revision
        assert store.status_counts()["resource_revision"] == 1
        captures = store.connection.execute("SELECT COUNT(*) FROM fetch_capture").fetchone()[0]
        assert captures == 2
    finally:
        store.close()


def test_store_versions_comments_only_when_comment_content_changes(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(_item())
        first, changed = store.upsert_comment(
            provider="hatena", resource_id=item.resource_id or "", author="alice", body="first", tags=["tag"], star_count=2
        )
        same, unchanged = store.upsert_comment(
            provider="hatena", resource_id=item.resource_id or "", author="alice", body="first", tags=["tag"], star_count=2
        )
        _, revised = store.upsert_comment(
            provider="hatena", resource_id=item.resource_id or "", author="alice", body="edited", tags=["tag"], star_count=3
        )

        assert first == same
        assert changed is True
        assert unchanged is False
        assert revised is True
        assert store.connection.execute("SELECT COUNT(*) FROM comment_revision").fetchone()[0] == 2
    finally:
        store.close()


def test_comment_refresh_preserves_an_existing_star_count(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(_item())
        store.upsert_comment(
            provider="hatena", resource_id=item.resource_id or "", author="alice", body="first", star_count=9,
            metadata={"timestamp": "one"},
        )
        store.upsert_comment(
            provider="hatena", resource_id=item.resource_id or "", author="alice", body="first", star_count=None,
            metadata={"timestamp": "two"},
        )
        current = store.connection.execute(
            """
            SELECT cr.star_count FROM comment AS c
            JOIN comment_revision AS cr ON cr.comment_revision_id = c.current_revision_id
            WHERE c.author = 'alice'
            """
        ).fetchone()[0]
        assert current == 9
    finally:
        store.close()


def test_store_imports_legacy_markdown_as_exact_bytes_once(tmp_path) -> None:
    raw = tmp_path / "raw" / "Hatena"
    raw.mkdir(parents=True)
    original = b"---\r\ntitle: legacy\r\n---\r\n\x93\xfa\x96{\r\n"
    legacy_file = raw / "old.md"
    legacy_file.write_bytes(original)
    store = VaultStore.open(tmp_path / ".feedian" / "feedian.sqlite3")
    try:
        imported, skipped = store.import_legacy_artifacts(tmp_path / "raw")
        assert (imported, skipped) == (1, 0)
        row = store.connection.execute("SELECT relative_path, content FROM legacy_artifact").fetchone()
        assert row["relative_path"] == "Hatena/old.md"
        assert row["content"] == original
        legacy_file.write_bytes(b"changed")
        assert store.import_legacy_artifacts(tmp_path / "raw") == (0, 1)
        assert store.connection.execute("SELECT content FROM legacy_artifact").fetchone()[0] == original
    finally:
        store.close()


def test_store_reuses_successful_llm_result_and_keeps_attempt_history(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(_item())
        revision, _ = store.record_resource_revision(item.resource_id or "", content_markdown="Body")
        first = store.start_llm_run(
            resource_id=item.resource_id or "", resource_revision_id=revision, operation="source-note", model="model",
            prompt_version="v1", input_fingerprint="fingerprint", request={"input": "one"},
        )
        store.finish_llm_run(first, result={"summary": "one"})
        second = store.start_llm_run(
            resource_id=item.resource_id or "", resource_revision_id=revision, operation="source-note", model="model",
            prompt_version="v1", input_fingerprint="fingerprint", request={"input": "two"},
        )
        store.finish_llm_run(second, error="retry")

        assert store.successful_llm_result(
            resource_revision_id=revision, operation="source-note", model="model", prompt_version="v1", input_fingerprint="fingerprint"
        ) == {"summary": "one"}
        assert store.connection.execute("SELECT COUNT(*) FROM llm_run").fetchone()[0] == 2
    finally:
        store.close()


def test_store_skips_recent_resource_fetch_unless_forced(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(_item())
        store.record_resource_revision(item.resource_id or "", content_markdown="Body")

        assert store.should_fetch_resource(item.resource_id or "", refresh_days=30) is False
        assert store.should_fetch_resource(item.resource_id or "", refresh_days=30, force=True) is True
    finally:
        store.close()
