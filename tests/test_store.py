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
        assert store.schema_version() == 4
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
        assert changed.source_revision_id == first.source_revision_id
        assert store.status_counts()["source_item_revision"] == 1
    finally:
        store.close()


def test_store_keeps_only_latest_content_and_fetch_payload(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        stored = store.upsert_canonical_item(_item())
        first_payload = store.put_payload(b"first pdf", media_type="application/pdf")
        revision, changed = store.record_resource_revision(
            stored.resource_id or "",
            content_markdown="First content",
            title="Title",
            http_payload_id=first_payload,
        )
        second_payload = store.put_payload(b"different pdf", media_type="application/pdf")
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
        assert captures == 1
        capture = store.connection.execute(
            """
            SELECT p.content FROM fetch_capture AS fc
            JOIN payload AS p ON p.payload_id = fc.http_payload_id
            """
        ).fetchone()
        assert capture["content"] == b"different pdf"
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
        assert store.connection.execute("SELECT COUNT(*) FROM comment_revision").fetchone()[0] == 1
    finally:
        store.close()


def test_comment_refresh_preserves_an_existing_star_count(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(_item())
        store.upsert_comment(
            provider="hatena", resource_id=item.resource_id or "", author="alice", body="first", star_count=9,
            posted_at="one",
        )
        store.upsert_comment(
            provider="hatena", resource_id=item.resource_id or "", author="alice", body="first", star_count=None,
            posted_at="two",
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


def test_store_upserts_a_resource_comment_batch_without_duplicate_revisions(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(_item())
        comments = [
            {"author": "alice", "body": "first", "tags": ["one"], "star_count": 4},
            {"author": "bob", "body": "second", "tags": ["two"]},
        ]

        first = store.upsert_comments(
            provider="hatena", resource_id=item.resource_id or "", comments=comments
        )
        second = store.upsert_comments(
            provider="hatena", resource_id=item.resource_id or "", comments=comments
        )

        assert [changed for _, changed in first] == [True, True]
        assert [changed for _, changed in second] == [False, False]
        assert store.connection.execute("SELECT COUNT(*) FROM comment").fetchone()[0] == 2
        assert store.connection.execute("SELECT COUNT(*) FROM comment_revision").fetchone()[0] == 2
        assert not store.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'comment_fts'"
        ).fetchone()
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


def test_v1_migration_prunes_history_images_and_inline_fts(tmp_path) -> None:
    path = tmp_path / "feedian.sqlite3"
    store = VaultStore.open(path)
    try:
        item = store.upsert_canonical_item(_item(), source_payload=b'{"current": true}')
        current_payload = store.put_payload(b"<html>current</html>", media_type="text/html")
        current_revision, _ = store.record_resource_revision(
            item.resource_id or "", content_markdown="Current body", http_payload_id=current_payload
        )
        comment_id, _ = store.upsert_comment(
            provider="hatena", resource_id=item.resource_id or "", author="alice", body="Current comment",
            metadata={"bookmark_count": 7},
        )
        image_payload = store.put_payload(b"image bytes", media_type="image/png")
        old_payload = store.put_payload(b"old bytes", media_type="text/html")
        source_item_id = item.source_item_id
        resource_id = item.resource_id or ""
        store.connection.executescript(
            f"""
            INSERT INTO source_item_revision(source_revision_id, source_item_id, metadata_json, metadata_hash, payload_id, created_at)
            VALUES ('old-source-revision', '{source_item_id}', '{{}}', 'old', '{old_payload}', '2000-01-01T00:00:00+00:00');
            INSERT INTO resource_revision(resource_revision_id, resource_id, title, content_markdown, discussion_text, content_hash, created_at)
            VALUES ('old-resource-revision', '{resource_id}', '', 'Old body', '', 'old', '2000-01-01T00:00:00+00:00');
            INSERT INTO fetch_capture(fetch_capture_id, resource_id, resource_revision_id, http_payload_id, final_url, fetched_at)
            VALUES ('old-capture', '{resource_id}', 'old-resource-revision', '{old_payload}', '', '2000-01-01T00:00:00+00:00');
            INSERT INTO comment_revision(comment_revision_id, comment_id, body, tags_json, metadata_json, content_hash, created_at)
            VALUES ('old-comment-revision', '{comment_id}', 'Old comment', '[]', '{{}}', 'old', '2000-01-01T00:00:00+00:00');
            INSERT INTO asset(asset_id, payload_id, resource_id, resource_revision_id, alt_text, source_url, created_at)
            VALUES ('old-asset', '{image_payload}', '{resource_id}', '{current_revision}', 'Hero', 'https://example.test/hero.png', '2026-01-01T00:00:00+00:00');
            CREATE VIRTUAL TABLE resource_fts USING fts5(resource_id UNINDEXED, title, content, tokenize='trigram');
            CREATE VIRTUAL TABLE source_fts USING fts5(source_item_id UNINDEXED, title, comment, tags, tokenize='trigram');
            CREATE VIRTUAL TABLE comment_fts USING fts5(comment_id UNINDEXED, body, tags, tokenize='trigram');
            CREATE TABLE legacy_artifact (
                legacy_artifact_id TEXT PRIMARY KEY, relative_path TEXT NOT NULL UNIQUE,
                content BLOB NOT NULL, sha256 TEXT NOT NULL, mtime_ns INTEGER, imported_at TEXT NOT NULL
            );
            INSERT INTO legacy_artifact VALUES ('legacy', 'Hatena/old.md', X'6F6C64', 'hash', 0, '2026-01-01');
            UPDATE schema_meta SET value = '1' WHERE key = 'schema_version';
            """
        )
    finally:
        store.close()

    try:
        VaultStore.open(path)
    except RuntimeError as exc:
        assert "migration is required" in str(exc)
    else:
        raise AssertionError("Schema 1 must require an explicit migration.")

    migrated = VaultStore.open(path, allow_migration=True)
    try:
        assert migrated.schema_version() == 4
        assert migrated.status_counts()["source_item_revision"] == 1
        assert migrated.status_counts()["resource_revision"] == 1
        assert migrated.connection.execute("SELECT COUNT(*) FROM fetch_capture").fetchone()[0] == 1
        assert migrated.connection.execute("SELECT COUNT(*) FROM comment_revision").fetchone()[0] == 1
        assert migrated.connection.execute("SELECT COUNT(*) FROM asset").fetchone()[0] == 0
        image = migrated.connection.execute(
            "SELECT source_url, alt_text FROM resource_image"
        ).fetchone()
        assert (image["source_url"], image["alt_text"]) == (
            "https://example.test/hero.png", "Hero"
        )
        assert migrated.comment_bookmark_count(resource_id) == 7
        assert migrated.connection.execute(
            "SELECT COUNT(*) FROM comment_revision WHERE star_count IS NOT NULL AND star_checked_at IS NULL"
        ).fetchone()[0] == 0
        assert not migrated.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name IN ('source_fts', 'resource_fts', 'comment_fts')"
        ).fetchone()
        assert migrated.connection.execute(
            "SELECT COUNT(*) FROM payload WHERE media_type LIKE 'image/%'"
        ).fetchone()[0] == 0
        assert migrated.connection.execute(
            "SELECT COUNT(*) FROM payload WHERE lower(media_type) LIKE '%html%'"
        ).fetchone()[0] == 0
        assert not migrated.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'legacy_artifact'"
        ).fetchone()
        assert migrated.integrity_check() == "ok"
    finally:
        migrated.close()


def test_v3_migration_normalizes_and_prunes_hatena_comments(tmp_path) -> None:
    path = tmp_path / "feedian.sqlite3"
    store = VaultStore.open(path)
    try:
        item = store.upsert_canonical_item(_item())
        resource_id = item.resource_id or ""
        store.update_comment_state(resource_id, 25)
        for index in range(25):
            store.upsert_comment(
                provider="hatena",
                resource_id=resource_id,
                author=f"user-{index:02d}",
                body=f"comment {index}",
                star_count=10 if index == 24 else 1,
                metadata={
                    "timestamp": f"2026/08/{index + 1:02d} 00:00",
                    "entry_url": "https://b.hatena.ne.jp/entry/example",
                    "entry_id": "123",
                    "star_url": "derived",
                    "bookmark_count": 25,
                },
            )
        store.connection.execute("UPDATE schema_meta SET value = '3' WHERE key = 'schema_version'")
        store.connection.commit()
    finally:
        store.close()

    migrated = VaultStore.open(path, allow_migration=True)
    try:
        rows = migrated.connection.execute(
            """
            SELECT c.author, cr.posted_at, cr.metadata_json
            FROM comment AS c
            JOIN comment_revision AS cr ON cr.comment_revision_id = c.current_revision_id
            ORDER BY COALESCE(cr.star_count, 0) DESC, cr.posted_at, c.comment_id
            """
        ).fetchall()
        assert len(rows) == 20
        assert rows[0]["author"] == "user-24"
        assert [row["author"] for row in rows[1:]] == [f"user-{index:02d}" for index in range(19)]
        assert all(row["posted_at"] for row in rows)
        assert all(row["metadata_json"] == "{}" for row in rows)
        state = migrated.connection.execute(
            "SELECT entry_url, entry_id FROM resource_comment_state"
        ).fetchone()
        assert tuple(state) == ("https://b.hatena.ne.jp/entry/example", "123")
    finally:
        migrated.close()
