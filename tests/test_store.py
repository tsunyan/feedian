from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from feedian.canonical import CanonicalItem
from feedian.store import VaultStore, _column_exists, _transaction


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


def _seed_failed_resource(
    store: VaultStore,
    *,
    url: str,
    warning: str = "HTTP 500",
    http_status: int | None = None,
    failure_kind: str | None = None,
    consecutive_failures: int | None = None,
    age: timedelta | None = None,
) -> str:
    """Create a resource whose only capture records a failed fetch.

    `consecutive_failures` and `age` override what `record_failed_fetch` itself
    would produce, via raw SQL -- the same pattern `test_record_failed_fetch_
    called_twice_advances_the_same_capture` already uses to backdate a capture.
    """
    item = store.upsert_canonical_item(
        CanonicalItem(source="hatena", source_id=url, content_key=f"url:{url}", url=url, title="Title")
    )
    resource_id = item.resource_id or ""
    store.record_failed_fetch(resource_id, warning=warning, http_status=http_status, failure_kind=failure_kind)
    if consecutive_failures is not None:
        store.connection.execute(
            "UPDATE fetch_capture SET consecutive_failures = ? WHERE resource_id = ?",
            (consecutive_failures, resource_id),
        )
    if age is not None:
        fetched_at = (datetime.now(timezone.utc) - age).isoformat()
        store.connection.execute(
            "UPDATE fetch_capture SET fetched_at = ? WHERE resource_id = ?", (fetched_at, resource_id)
        )
    store.connection.commit()
    return resource_id


def test_store_creates_schema_and_deduplicates_payload(tmp_path) -> None:
    store = VaultStore.open(tmp_path / ".feedian" / "feedian.sqlite3")
    try:
        first = store.put_payload(b"<html>same</html>", media_type="text/html")
        second = store.put_payload(b"<html>same</html>", media_type="text/html")

        assert first == second
        assert store.schema_version() == 9
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


def test_resource_fetch_validators_stay_blank_until_a_body_is_held(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(_item())
        resource_id = item.resource_id or ""

        # A failed fetch still records the response's ETag, but the resource
        # never held a body: current_revision_id stays NULL.
        store.record_failed_fetch(resource_id, warning="HTTP 500", response_headers={"ETag": "stale-etag"})
        assert (
            store.connection.execute(
                "SELECT current_revision_id FROM resource WHERE resource_id = ?", (resource_id,)
            ).fetchone()["current_revision_id"]
            is None
        )
        assert store.resource_fetch_validators(resource_id) == ("", "")

        # current_revision_id is set, but the revision itself is blank.
        store.record_resource_revision(
            resource_id, content_markdown="   ", response_headers={"ETag": "blank-etag"}
        )
        assert store.resource_fetch_validators(resource_id) == ("", "")

        # A real body: the validators are now the latest capture's.
        store.record_resource_revision(
            resource_id,
            content_markdown="Real body",
            response_headers={"ETag": "real-etag", "Last-Modified": "real-last-modified"},
        )
        assert store.resource_fetch_validators(resource_id) == ("real-etag", "real-last-modified")
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
        assert migrated.schema_version() == 9
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


def test_transaction_discards_every_write_when_the_batch_fails(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        with pytest.raises(RuntimeError):
            with _transaction(store.connection) as connection:
                connection.execute(
                    "INSERT INTO schema_meta(key, value) VALUES ('probe', 'written')"
                )
                raise RuntimeError("batch failed")

        assert store.connection.execute(
            "SELECT COUNT(*) FROM schema_meta WHERE key = 'probe'"
        ).fetchone()[0] == 0
    finally:
        store.close()


def _downgrade_to_v4(path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            ALTER TABLE fetch_capture DROP COLUMN response_etag;
            ALTER TABLE fetch_capture DROP COLUMN response_last_modified;
            UPDATE schema_meta SET value = '4' WHERE key = 'schema_version';
            """
        )
        connection.commit()
    finally:
        connection.close()


def test_v4_migration_adds_fetch_validators_and_commits(tmp_path) -> None:
    path = tmp_path / "feedian.sqlite3"
    VaultStore.open(path).close()
    _downgrade_to_v4(path)

    migrated = VaultStore.open(path, allow_migration=True)
    try:
        columns = {
            str(row[1]) for row in migrated.connection.execute("PRAGMA table_info(fetch_capture)")
        }
        assert {"response_etag", "response_last_modified"} <= columns
        assert migrated.schema_version() == 9
    finally:
        migrated.close()


def test_v5_migration_backfills_llm_backend_audit_columns(tmp_path) -> None:
    path = tmp_path / "feedian.sqlite3"
    store = VaultStore.open(path)
    try:
        item = store.upsert_canonical_item(_item())
        revision, _ = store.record_resource_revision(
            item.resource_id or "", content_markdown="Body"
        )
        run_id = store.start_llm_run(
            resource_id=item.resource_id or "",
            resource_revision_id=revision,
            operation="source-note",
            model="gpt-test",
            prompt_version="v1",
            input_fingerprint="legacy-fingerprint",
            request={"provider": "openai", "input": [], "text": {}},
        )
        store.finish_llm_run(run_id, result={"summary": "one"})
    finally:
        store.close()

    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP INDEX IF EXISTS llm_run_reuse_idx")
        for column in (
            "backend",
            "summary_schema_version",
            "fingerprint_version",
            "auth_mode",
            "billing_mode",
            "backend_metadata_json",
            "duration_ms",
        ):
            connection.execute(f"ALTER TABLE llm_run DROP COLUMN {column}")
        connection.execute(
            "UPDATE schema_meta SET value = '5' WHERE key = 'schema_version'"
        )
        connection.commit()
    finally:
        connection.close()

    migrated = VaultStore.open(path, allow_migration=True)
    try:
        row = migrated.connection.execute(
            """
            SELECT backend, summary_schema_version, fingerprint_version,
                   auth_mode, billing_mode, backend_metadata_json, duration_ms
            FROM llm_run
            WHERE llm_run_id = ?
            """,
            (run_id,),
        ).fetchone()
        assert migrated.schema_version() == 9
        assert tuple(row) == (
            "openai-responses",
            "1",
            1,
            "api-key",
            "metered-api",
            "{}",
            None,
        )
    finally:
        migrated.close()


def test_failed_v4_migration_leaves_the_database_at_version_four(tmp_path, monkeypatch) -> None:
    path = tmp_path / "feedian.sqlite3"
    VaultStore.open(path).close()
    _downgrade_to_v4(path)

    def fail_on_the_second_column(connection, table, column):
        if column == "response_last_modified":
            raise RuntimeError("migration step failed")
        return _column_exists(connection, table, column)

    # Fail after the first ALTER has already run inside the transaction.
    monkeypatch.setattr("feedian.store._column_exists", fail_on_the_second_column)
    with pytest.raises(RuntimeError, match="migration step failed"):
        VaultStore.open(path, allow_migration=True)

    connection = sqlite3.connect(path)
    try:
        version = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(fetch_capture)")}
    finally:
        connection.close()

    assert version == "4"
    assert "response_etag" not in columns


def test_a_migrated_database_has_the_same_llm_run_columns_as_a_fresh_one(tmp_path) -> None:
    """Same schema_version must mean the same table, however it got there.

    SQLite needs a default when a NOT NULL column joins a populated table, so the
    migration has to supply one; a fresh database must declare the same defaults
    or the two diverge under one version number.
    """

    def llm_run_columns(path) -> dict[str, tuple]:
        store = VaultStore.open(path, allow_migration=True)
        try:
            assert store.schema_version() == 9
            rows = store.connection.execute("PRAGMA table_info(llm_run)").fetchall()
            return {row[1]: (row[2], row[3], row[5]) for row in rows}
        finally:
            store.close()

    fresh_path = tmp_path / "fresh.sqlite3"
    fresh_columns = llm_run_columns(fresh_path)

    migrated_path = tmp_path / "migrated.sqlite3"
    VaultStore.open(migrated_path).close()
    connection = sqlite3.connect(migrated_path)
    try:
        connection.execute("DROP INDEX IF EXISTS llm_run_reuse_idx")
        for column in (
            "backend", "summary_schema_version", "fingerprint_version",
            "auth_mode", "billing_mode", "backend_metadata_json", "duration_ms",
        ):
            connection.execute(f"ALTER TABLE llm_run DROP COLUMN {column}")
        connection.execute("UPDATE schema_meta SET value = '5' WHERE key = 'schema_version'")
        connection.commit()
    finally:
        connection.close()

    assert llm_run_columns(migrated_path) == fresh_columns


def test_record_failed_fetch_without_a_revision_leaves_it_absent(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(_item())
        resource_id = item.resource_id or ""

        store.record_failed_fetch(resource_id, warning="fetch timed out")

        capture = store.connection.execute(
            "SELECT warning FROM fetch_capture WHERE resource_id = ?", (resource_id,)
        ).fetchone()
        assert capture is not None
        assert capture["warning"] == "fetch timed out"
        current_revision_id = store.connection.execute(
            "SELECT current_revision_id FROM resource WHERE resource_id = ?", (resource_id,)
        ).fetchone()["current_revision_id"]
        assert current_revision_id is None
        assert store.connection.execute(
            "SELECT COUNT(*) FROM resource_revision WHERE resource_id = ?", (resource_id,)
        ).fetchone()[0] == 0
    finally:
        store.close()


def test_record_failed_fetch_nulls_current_revision_but_keeps_the_row(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(_item())
        resource_id = item.resource_id or ""
        revision_id, _ = store.record_resource_revision(resource_id, content_markdown="   ")

        store.record_failed_fetch(resource_id, warning="empty extraction")

        current_revision_id = store.connection.execute(
            "SELECT current_revision_id FROM resource WHERE resource_id = ?", (resource_id,)
        ).fetchone()["current_revision_id"]
        assert current_revision_id is None
        still_there = store.connection.execute(
            "SELECT resource_revision_id FROM resource_revision WHERE resource_revision_id = ?", (revision_id,)
        ).fetchone()
        assert still_there is not None
    finally:
        store.close()


def test_record_failed_fetch_on_a_good_revision_leaves_it_untouched(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(_item())
        resource_id = item.resource_id or ""
        revision_id, _ = store.record_resource_revision(resource_id, content_markdown="Real content")

        store.record_failed_fetch(resource_id, warning="refresh failed")

        row = store.connection.execute(
            "SELECT current_revision_id FROM resource WHERE resource_id = ?", (resource_id,)
        ).fetchone()
        assert row["current_revision_id"] == revision_id
        content = store.connection.execute(
            "SELECT content_markdown FROM resource_revision WHERE resource_revision_id = ?", (revision_id,)
        ).fetchone()["content_markdown"]
        assert content == "Real content"
    finally:
        store.close()


def test_record_failed_fetch_called_twice_advances_the_same_capture(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(_item())
        resource_id = item.resource_id or ""

        store.record_failed_fetch(resource_id, warning="first failure")
        store.connection.execute(
            "UPDATE fetch_capture SET fetched_at = ? WHERE resource_id = ?",
            ("2000-01-01T00:00:00+00:00", resource_id),
        )
        store.connection.commit()

        store.record_failed_fetch(resource_id, warning="second failure")

        rows = store.connection.execute(
            "SELECT fetched_at, warning FROM fetch_capture WHERE resource_id = ?", (resource_id,)
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["warning"] == "second failure"
        assert rows[0]["fetched_at"] > "2000-01-01T00:00:00+00:00"
    finally:
        store.close()


def test_record_failed_fetch_keeps_the_http_payload_alive(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(_item())
        resource_id = item.resource_id or ""
        payload_id = store.put_payload(b"raw bytes", media_type="text/html")

        store.record_failed_fetch(resource_id, warning="extraction failed", http_payload_id=payload_id)
        store.delete_orphan_payloads()

        assert store.connection.execute(
            "SELECT 1 FROM payload WHERE payload_id = ?", (payload_id,)
        ).fetchone() is not None
    finally:
        store.close()


def test_record_failed_fetch_raises_for_an_unknown_resource(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        with pytest.raises(KeyError):
            store.record_failed_fetch("does-not-exist", warning="unreachable")
    finally:
        store.close()


def test_record_failed_fetch_survives_an_llm_run_referencing_the_revision(tmp_path) -> None:
    """`foreign_keys=ON` means a stray DELETE of resource_revision would raise here."""
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(_item())
        resource_id = item.resource_id or ""
        revision_id, _ = store.record_resource_revision(resource_id, content_markdown="")
        run_id = store.start_llm_run(
            resource_id=resource_id, resource_revision_id=revision_id, operation="source-note",
            model="model", prompt_version="v1", input_fingerprint="fp", request={"input": "x"},
        )

        store.record_failed_fetch(resource_id, warning="refresh failed")

        assert store.connection.execute(
            "SELECT 1 FROM llm_run WHERE llm_run_id = ?", (run_id,)
        ).fetchone() is not None
        assert store.connection.execute(
            "SELECT 1 FROM resource_revision WHERE resource_revision_id = ?", (revision_id,)
        ).fetchone() is not None
    finally:
        store.close()


def test_unfetched_resources_includes_null_revision_and_excludes_real_content(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        missing = store.upsert_canonical_item(
            CanonicalItem(
                source="hatena", source_id="missing", content_key="url:missing",
                url="https://example.test/missing", title="Missing",
            )
        )
        present = store.upsert_canonical_item(
            CanonicalItem(
                source="hatena", source_id="present", content_key="url:present",
                url="https://example.test/present", title="Present",
            )
        )
        store.record_resource_revision(present.resource_id or "", content_markdown="Real content")

        results = store.unfetched_resources(["hatena"])

        resource_ids = {resource_id for resource_id, _ in results}
        assert missing.resource_id in resource_ids
        assert present.resource_id not in resource_ids
    finally:
        store.close()


def test_unfetched_resources_includes_the_legacy_blank_plus_warning_case(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(
            CanonicalItem(
                source="hatena", source_id="legacy", content_key="url:legacy",
                url="https://example.test/legacy", title="Legacy",
            )
        )
        resource_id = item.resource_id or ""
        store.record_resource_revision(resource_id, content_markdown="   ")
        # Simulate a row written before record_failed_fetch existed: the capture
        # carries a warning but current_revision_id was never cleared.
        store.connection.execute(
            "UPDATE fetch_capture SET warning = 'stale warning' WHERE resource_id = ?", (resource_id,)
        )
        store.connection.commit()
        assert store.connection.execute(
            "SELECT current_revision_id FROM resource WHERE resource_id = ?", (resource_id,)
        ).fetchone()["current_revision_id"] is not None

        results = store.unfetched_resources(["hatena"])

        assert resource_id in {rid for rid, _ in results}
    finally:
        store.close()


def test_unfetched_resources_excludes_a_resource_without_a_url_identifier(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(
            CanonicalItem(source="hatena", source_id="no-url", content_key="no-url", url="", title="No URL")
        )
        resource_id = item.resource_id or ""
        assert store.connection.execute(
            "SELECT current_revision_id FROM resource WHERE resource_id = ?", (resource_id,)
        ).fetchone()["current_revision_id"] is None

        results = store.unfetched_resources(["hatena"])

        assert resource_id not in {rid for rid, _ in results}
    finally:
        store.close()


def test_unfetched_resources_orders_never_fetched_first_then_oldest_capture(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        never = store.upsert_canonical_item(
            CanonicalItem(
                source="hatena", source_id="never", content_key="url:never",
                url="https://example.test/never", title="Never",
            )
        )
        older = store.upsert_canonical_item(
            CanonicalItem(
                source="hatena", source_id="older", content_key="url:older",
                url="https://example.test/older", title="Older",
            )
        )
        newer = store.upsert_canonical_item(
            CanonicalItem(
                source="hatena", source_id="newer", content_key="url:newer",
                url="https://example.test/newer", title="Newer",
            )
        )
        store.record_failed_fetch(older.resource_id or "", warning="w")
        store.record_failed_fetch(newer.resource_id or "", warning="w")
        store.connection.execute(
            "UPDATE fetch_capture SET fetched_at = ? WHERE resource_id = ?",
            ("2020-01-01T00:00:00+00:00", older.resource_id),
        )
        store.connection.execute(
            "UPDATE fetch_capture SET fetched_at = ? WHERE resource_id = ?",
            ("2020-06-01T00:00:00+00:00", newer.resource_id),
        )
        store.connection.commit()

        results = store.unfetched_resources(["hatena"])

        assert [resource_id for resource_id, _ in results] == [
            never.resource_id, older.resource_id, newer.resource_id,
        ]
    finally:
        store.close()


def test_unfetched_resources_filters_by_provider(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        wanted = store.upsert_canonical_item(
            CanonicalItem(
                source="hatena", source_id="wanted", content_key="url:wanted",
                url="https://example.test/wanted", title="Wanted",
            )
        )
        other = store.upsert_canonical_item(
            CanonicalItem(
                source="pocket", source_id="other", content_key="url:other",
                url="https://example.test/other", title="Other",
            )
        )

        results = store.unfetched_resources(["hatena"])

        resource_ids = {resource_id for resource_id, _ in results}
        assert wanted.resource_id in resource_ids
        assert other.resource_id not in resource_ids
    finally:
        store.close()


def test_unfetched_resources_dedupes_a_resource_shared_by_two_providers(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        first = store.upsert_canonical_item(
            CanonicalItem(
                source="hatena", source_id="shared", content_key="url:shared",
                url="https://example.test/shared", title="Shared",
            )
        )
        second = store.upsert_canonical_item(
            CanonicalItem(
                source="raindrop", source_id="shared", content_key="url:shared",
                url="https://example.test/shared", title="Shared",
            )
        )
        assert first.resource_id == second.resource_id

        results = store.unfetched_resources(["hatena", "raindrop"])

        assert [resource_id for resource_id, _ in results].count(first.resource_id) == 1
    finally:
        store.close()


def test_known_native_ids_is_scoped_to_provider(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        store.upsert_canonical_item(
            CanonicalItem(
                source="hatena", source_id="hatena-a", content_key="url:a",
                url="https://example.test/a", title="A",
            )
        )
        store.upsert_canonical_item(
            CanonicalItem(
                source="hatena", source_id="hatena-b", content_key="url:b",
                url="https://example.test/b", title="B",
            )
        )
        store.upsert_canonical_item(
            CanonicalItem(
                source="raindrop", source_id="raindrop-a", content_key="url:c",
                url="https://example.test/c", title="C",
            )
        )

        hatena_ids = store.known_native_ids("hatena")

        assert hatena_ids == {"hatena-a", "hatena-b"}
        assert "raindrop-a" not in hatena_ids
    finally:
        store.close()


def test_source_items_for_resource_filters_by_provider(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        hatena_item = store.upsert_canonical_item(
            CanonicalItem(
                source="hatena", source_id="hatena-1", content_key="url:shared",
                url="https://example.test/shared", title="Shared",
            )
        )
        raindrop_item = store.upsert_canonical_item(
            CanonicalItem(
                source="raindrop", source_id="raindrop-1", content_key="url:shared",
                url="https://example.test/shared", title="Shared",
            )
        )
        store.upsert_canonical_item(
            CanonicalItem(
                source="pocket", source_id="pocket-1", content_key="url:shared",
                url="https://example.test/shared", title="Shared",
            )
        )
        resource_id = hatena_item.resource_id or ""

        result = store.source_items_for_resource(resource_id, ["hatena", "raindrop"])

        assert set(result) == {hatena_item.source_item_id, raindrop_item.source_item_id}
    finally:
        store.close()


def test_create_sync_run_persists_mode_and_rejects_unknown_modes(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        run_id = store.create_sync_run(["hatena"], "fingerprint", mode="quick")

        mode = store.connection.execute(
            "SELECT mode FROM sync_run WHERE sync_run_id = ?", (run_id,)
        ).fetchone()["mode"]
        assert mode == "quick"

        with pytest.raises(ValueError):
            store.create_sync_run(["hatena"], "fingerprint", mode="partial")
    finally:
        store.close()


def test_latest_provider_sync_run_defaults_to_full_mode(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        full_run = store.create_sync_run(["hatena"], "fp", mode="full")
        store.finish_sync_run(full_run, status="completed")
        quick_run = store.create_sync_run(["hatena"], "fp", mode="quick")
        store.finish_sync_run(quick_run, status="completed")

        default_row = store.latest_provider_sync_run("hatena")
        assert default_row is not None
        assert default_row["sync_run_id"] == full_run
        assert default_row["mode"] == "full"

        quick_row = store.latest_provider_sync_run("hatena", mode="quick")
        assert quick_row is not None
        assert quick_row["sync_run_id"] == quick_run
    finally:
        store.close()


def _downgrade_to_v6(path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            ALTER TABLE sync_run DROP COLUMN mode;
            UPDATE schema_meta SET value = '6' WHERE key = 'schema_version';
            """
        )
        connection.commit()
    finally:
        connection.close()


def test_v6_migration_backfills_full_mode_on_existing_sync_runs(tmp_path) -> None:
    path = tmp_path / "feedian.sqlite3"
    store = VaultStore.open(path)
    try:
        run_id = store.create_sync_run(["hatena"], "fp")
        store.finish_sync_run(run_id, status="completed")
    finally:
        store.close()
    _downgrade_to_v6(path)

    migrated = VaultStore.open(path, allow_migration=True)
    try:
        assert migrated.schema_version() == 9
        mode = migrated.connection.execute(
            "SELECT mode FROM sync_run WHERE sync_run_id = ?", (run_id,)
        ).fetchone()["mode"]
        assert mode == "full"
    finally:
        migrated.close()


def test_should_fetch_resource_backoff_grows_with_consecutive_failures(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        # n=1: threshold is retry_base_minutes * 2**0 = 30 minutes.
        n1_not_due = _seed_failed_resource(
            store, url="https://example.test/n1-not-due", consecutive_failures=1, age=timedelta(minutes=29)
        )
        n1_due = _seed_failed_resource(
            store, url="https://example.test/n1-due", consecutive_failures=1, age=timedelta(minutes=31)
        )
        # n=3: threshold is retry_base_minutes * 2**2 = 120 minutes.
        n3_not_due = _seed_failed_resource(
            store, url="https://example.test/n3-not-due", consecutive_failures=3, age=timedelta(minutes=119)
        )
        n3_due = _seed_failed_resource(
            store, url="https://example.test/n3-due", consecutive_failures=3, age=timedelta(minutes=121)
        )

        assert store.should_fetch_resource(n1_not_due, refresh_days=30) is False
        assert store.should_fetch_resource(n1_due, refresh_days=30) is True
        assert store.should_fetch_resource(n3_not_due, refresh_days=30) is False
        assert store.should_fetch_resource(n3_due, refresh_days=30) is True
    finally:
        store.close()


def test_should_fetch_resource_backoff_is_capped_at_retry_max_days(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        # An uncapped exponent for n=40 would put the threshold decades away;
        # the cap keeps it at retry_max_days regardless.
        resource_id = _seed_failed_resource(
            store, url="https://example.test/capped", consecutive_failures=40, age=timedelta(days=30, minutes=5)
        )

        assert store.should_fetch_resource(resource_id, refresh_days=30, retry_max_days=30) is True
    finally:
        store.close()


def test_record_resource_revision_resets_consecutive_failures_on_success(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(_item())
        resource_id = item.resource_id or ""
        store.record_failed_fetch(resource_id, warning="HTTP 500")
        store.record_failed_fetch(resource_id, warning="HTTP 500")

        store.record_resource_revision(resource_id, content_markdown="Recovered body")

        consecutive_failures = store.connection.execute(
            "SELECT consecutive_failures FROM fetch_capture WHERE resource_id = ?", (resource_id,)
        ).fetchone()["consecutive_failures"]
        assert consecutive_failures == 0
    finally:
        store.close()


def test_record_resource_revision_keeps_the_callers_warning_on_a_recovered_body(tmp_path) -> None:
    """The RSS fallback records a body alongside the page-fetch error it stood in for."""
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(_item())
        resource_id = item.resource_id or ""

        store.record_resource_revision(resource_id, content_markdown="RSS fallback body", warning="HTTP 503")

        warning = store.connection.execute(
            "SELECT warning FROM fetch_capture WHERE resource_id = ?", (resource_id,)
        ).fetchone()["warning"]
        assert warning == "HTTP 503"
    finally:
        store.close()


def test_should_fetch_resource_force_overrides_a_terminal_status(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        resource_id = _seed_failed_resource(
            store, url="https://example.test/gone", http_status=404, consecutive_failures=99
        )

        assert store.should_fetch_resource(resource_id, refresh_days=30, force=True) is True
    finally:
        store.close()


def test_should_fetch_resource_suppresses_a_terminal_status_regardless_of_age(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        resource_id = _seed_failed_resource(
            store, url="https://example.test/gone", http_status=404, consecutive_failures=1, age=timedelta(days=365)
        )

        assert store.should_fetch_resource(resource_id, refresh_days=30) is False
    finally:
        store.close()


def test_record_failed_fetch_keeps_an_existing_body_after_a_terminal_status(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(_item())
        resource_id = item.resource_id or ""
        store.record_resource_revision(resource_id, content_markdown="Existing body")

        store.record_failed_fetch(resource_id, warning="HTTP 404", http_status=404)

        content = store.connection.execute(
            "SELECT content_markdown FROM resource_revision WHERE resource_id = ?", (resource_id,)
        ).fetchone()["content_markdown"]
        assert content == "Existing body"
        current_revision_id = store.connection.execute(
            "SELECT current_revision_id FROM resource WHERE resource_id = ?", (resource_id,)
        ).fetchone()["current_revision_id"]
        assert current_revision_id is not None
    finally:
        store.close()


def test_record_failed_fetch_stores_null_http_status_when_none_and_no_prior_status(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(_item())
        resource_id = item.resource_id or ""

        store.record_failed_fetch(resource_id, warning="timed out")

        http_status = store.connection.execute(
            "SELECT http_status FROM fetch_capture WHERE resource_id = ?", (resource_id,)
        ).fetchone()["http_status"]
        assert http_status is None
    finally:
        store.close()


def test_record_failed_fetch_replaces_a_terminal_status_with_a_later_transient_one(tmp_path) -> None:
    """http_status is the latest failure's state, not a value to preserve.

    Keeping the 404 would leave the terminal rule suppressing a resource whose
    current failure is a timeout, with no route back to the backoff.
    """
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(_item())
        resource_id = item.resource_id or ""

        store.record_failed_fetch(resource_id, warning="not found", http_status=404)
        assert store.connection.execute(
            "SELECT http_status FROM fetch_capture WHERE resource_id = ?", (resource_id,)
        ).fetchone()["http_status"] == 404

        store.record_failed_fetch(resource_id, warning="timed out", http_status=None)

        assert store.connection.execute(
            "SELECT http_status FROM fetch_capture WHERE resource_id = ?", (resource_id,)
        ).fetchone()["http_status"] is None
    finally:
        store.close()


def test_a_transient_failure_after_a_terminal_one_becomes_due_again(tmp_path) -> None:
    """404 -> --force-fetch -> a non-HTTP failure must return to the backoff."""
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(_item())
        resource_id = item.resource_id or ""
        store.record_failed_fetch(resource_id, warning="not found", http_status=404)
        assert store.should_fetch_resource(resource_id, refresh_days=30) is False

        store.record_failed_fetch(resource_id, warning="DNS failure", http_status=None)
        store.connection.execute(
            "UPDATE fetch_capture SET fetched_at = ? WHERE resource_id = ?",
            ("2000-01-01T00:00:00+00:00", resource_id),
        )
        store.connection.commit()

        assert store.should_fetch_resource(resource_id, refresh_days=30) is True
    finally:
        store.close()

def test_record_not_modified_fetch_clears_failure_state_and_sets_304(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(_item())
        resource_id = item.resource_id or ""
        store.record_resource_revision(resource_id, content_markdown="Body")
        store.record_failed_fetch(resource_id, warning="HTTP 500", http_status=500)

        store.record_not_modified_fetch(resource_id, final_url="https://example.test/article")

        row = store.connection.execute(
            "SELECT consecutive_failures, warning, http_status FROM fetch_capture WHERE resource_id = ?",
            (resource_id,),
        ).fetchone()
        assert row["consecutive_failures"] == 0
        assert row["warning"] is None
        assert row["http_status"] == 304
    finally:
        store.close()


def test_consecutive_failures_after_a_success_failure_not_modified_failure_sequence(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(_item())
        resource_id = item.resource_id or ""

        store.record_resource_revision(resource_id, content_markdown="Body")  # success
        store.record_failed_fetch(resource_id, warning="HTTP 500")  # failure: 1
        store.record_not_modified_fetch(resource_id, final_url="https://example.test/article")  # 304: resets to 0
        store.record_failed_fetch(resource_id, warning="HTTP 500 again")  # failure: 1

        consecutive_failures = store.connection.execute(
            "SELECT consecutive_failures FROM fetch_capture WHERE resource_id = ?", (resource_id,)
        ).fetchone()["consecutive_failures"]
        assert consecutive_failures == 1
    finally:
        store.close()


def _downgrade_to_v7(path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            ALTER TABLE fetch_capture DROP COLUMN consecutive_failures;
            ALTER TABLE fetch_capture DROP COLUMN http_status;
            UPDATE schema_meta SET value = '7' WHERE key = 'schema_version';
            """
        )
        connection.commit()
    finally:
        connection.close()


def test_v7_migration_backfills_retry_state_on_a_failed_capture(tmp_path) -> None:
    path = tmp_path / "feedian.sqlite3"
    store = VaultStore.open(path)
    try:
        item = store.upsert_canonical_item(_item())
        resource_id = item.resource_id or ""
        # A resource with no body, left behind by a failed fetch before
        # consecutive_failures/http_status tracking existed.
        store.record_failed_fetch(resource_id, warning="HTTP 500")
        store.connection.execute(
            "UPDATE fetch_capture SET fetched_at = ? WHERE resource_id = ?",
            ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), resource_id),
        )
        store.connection.commit()
    finally:
        store.close()
    _downgrade_to_v7(path)

    migrated = VaultStore.open(path, allow_migration=True)
    try:
        assert migrated.schema_version() == 9
        columns = {
            str(row[1]) for row in migrated.connection.execute("PRAGMA table_info(fetch_capture)")
        }
        assert {"consecutive_failures", "http_status"} <= columns

        row = migrated.connection.execute(
            "SELECT consecutive_failures, http_status FROM fetch_capture WHERE resource_id = ?",
            (resource_id,),
        ).fetchone()
        assert row["consecutive_failures"] == 0
        assert row["http_status"] is None

        # A minute old is nowhere near due by refresh_days=30 or by any real
        # backoff -- only the migrated-row special case (a warning but no
        # recorded failure count) makes this True.
        assert migrated.should_fetch_resource(resource_id, refresh_days=30) is True
    finally:
        migrated.close()


def test_should_fetch_resource_retries_a_migrated_failure_row_immediately(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(_item())
        resource_id = item.resource_id or ""
        store.record_failed_fetch(resource_id, warning="HTTP 500")
        # Simulate a capture written before consecutive_failures tracking existed:
        # a warning but no counter, aged only a minute.
        store.connection.execute(
            "UPDATE fetch_capture SET consecutive_failures = 0, fetched_at = ? WHERE resource_id = ?",
            ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), resource_id),
        )
        store.connection.commit()

        assert store.should_fetch_resource(resource_id, refresh_days=30) is True
    finally:
        store.close()


def test_should_fetch_resource_migrated_row_with_a_body_follows_refresh_days(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(_item())
        resource_id = item.resource_id or ""
        store.record_resource_revision(resource_id, content_markdown="Body", warning="stale warning")
        store.connection.execute(
            "UPDATE fetch_capture SET consecutive_failures = 0, fetched_at = ? WHERE resource_id = ?",
            ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), resource_id),
        )
        store.connection.commit()

        # A minute old and refresh_days=30: due only if the failure/backoff path
        # were wrongly applied instead of the held-body refresh_days rule.
        assert store.should_fetch_resource(resource_id, refresh_days=30) is False
    finally:
        store.close()


def test_terminal_failure_count_counts_only_matching_bodyless_resources(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        _seed_failed_resource(store, url="https://example.test/gone", http_status=404)
        recovered = store.upsert_canonical_item(
            CanonicalItem(
                source="hatena", source_id="recovered", content_key="url:recovered",
                url="https://example.test/recovered", title="Recovered",
            )
        )
        store.record_resource_revision(
            recovered.resource_id or "", content_markdown="Body", warning="HTTP 404", http_status=404
        )
        _seed_failed_resource(store, url="https://example.test/other", http_status=500)

        assert store.terminal_failure_count((404, 410)) == 1
    finally:
        store.close()


def test_terminal_failure_count_agrees_with_should_fetch_resource_on_a_payload_capture(tmp_path) -> None:
    """A capture holding raw bytes is still refetched, so it is not "given up on".

    A PDF that later 404s keeps the payload an earlier attempt stored, which
    sends should_fetch_resource down the refresh_days branch. Counting it as
    unreachable would have `feedian status` claim Feedian stopped retrying a URL
    it goes on fetching every refresh_days.
    """
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(_item())
        resource_id = item.resource_id or ""
        payload_id = store.put_payload(b"%PDF-1.4", media_type="application/pdf")
        store.record_failed_fetch(
            resource_id, warning="unsupported content type", http_payload_id=payload_id, http_status=200
        )
        store.record_failed_fetch(resource_id, warning="HTTP 404", http_status=404)
        store.connection.execute(
            "UPDATE fetch_capture SET fetched_at = ? WHERE resource_id = ?",
            ("2000-01-01T00:00:00+00:00", resource_id),
        )
        store.connection.commit()

        assert store.should_fetch_resource(resource_id, refresh_days=30) is True
        assert store.terminal_failure_count((404, 410)) == 0
    finally:
        store.close()


def test_terminal_failure_count_returns_zero_for_an_empty_status_tuple(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        _seed_failed_resource(store, url="https://example.test/gone", http_status=404)

        assert store.terminal_failure_count(()) == 0
    finally:
        store.close()


def test_should_fetch_resource_with_no_terminal_statuses_falls_through_to_backoff(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        not_due = _seed_failed_resource(
            store, url="https://example.test/gone-1", http_status=404, consecutive_failures=1,
            age=timedelta(minutes=1),
        )
        due = _seed_failed_resource(
            store, url="https://example.test/gone-2", http_status=404, consecutive_failures=1,
            age=timedelta(minutes=31),
        )

        assert store.should_fetch_resource(not_due, refresh_days=30, terminal_http_statuses=()) is False
        assert store.should_fetch_resource(due, refresh_days=30, terminal_http_statuses=()) is True
    finally:
        store.close()


def test_record_failed_fetch_stores_failure_kind(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(_item())
        resource_id = item.resource_id or ""

        store.record_failed_fetch(resource_id, warning="hostname could not be resolved", failure_kind="dns")

        failure_kind = store.connection.execute(
            "SELECT failure_kind FROM fetch_capture WHERE resource_id = ?", (resource_id,)
        ).fetchone()["failure_kind"]
        assert failure_kind == "dns"
    finally:
        store.close()


def test_record_resource_revision_resets_failure_kind_on_success(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(_item())
        resource_id = item.resource_id or ""
        store.record_failed_fetch(resource_id, warning="hostname could not be resolved", failure_kind="dns")

        store.record_resource_revision(resource_id, content_markdown="Recovered body")

        failure_kind = store.connection.execute(
            "SELECT failure_kind FROM fetch_capture WHERE resource_id = ?", (resource_id,)
        ).fetchone()["failure_kind"]
        assert failure_kind is None
    finally:
        store.close()


def test_record_not_modified_fetch_resets_failure_kind(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(_item())
        resource_id = item.resource_id or ""
        store.record_resource_revision(resource_id, content_markdown="Body")
        store.record_failed_fetch(resource_id, warning="connection timed out", failure_kind="timeout")

        store.record_not_modified_fetch(resource_id, final_url="https://example.test/article")

        failure_kind = store.connection.execute(
            "SELECT failure_kind FROM fetch_capture WHERE resource_id = ?", (resource_id,)
        ).fetchone()["failure_kind"]
        assert failure_kind is None
    finally:
        store.close()


def test_record_failed_fetch_does_not_coalesce_failure_kind(tmp_path) -> None:
    """failure_kind is the latest failure's state, like http_status: a later

    failure of a different (or no) kind must not leave the old one in place,
    or the terminal rule would go on suppressing a resource for a reason that
    no longer holds.
    """
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(_item())
        resource_id = item.resource_id or ""
        store.record_failed_fetch(resource_id, warning="hostname could not be resolved", failure_kind="dns")
        assert (
            store.connection.execute(
                "SELECT failure_kind FROM fetch_capture WHERE resource_id = ?", (resource_id,)
            ).fetchone()["failure_kind"]
            == "dns"
        )

        store.record_failed_fetch(resource_id, warning="HTTP 500")

        failure_kind = store.connection.execute(
            "SELECT failure_kind FROM fetch_capture WHERE resource_id = ?", (resource_id,)
        ).fetchone()["failure_kind"]
        assert failure_kind is None
    finally:
        store.close()


def test_should_fetch_resource_suppresses_terminal_failure_kind_at_threshold(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        for kind in ("dns", "timeout"):
            resource_id = _seed_failed_resource(
                store,
                url=f"https://example.test/{kind}-terminal",
                failure_kind=kind,
                consecutive_failures=3,
                age=timedelta(minutes=1),
            )
            assert store.should_fetch_resource(resource_id, refresh_days=30) is False, kind
    finally:
        store.close()


def test_should_fetch_resource_retries_below_terminal_kind_threshold(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        for kind in ("dns", "timeout"):
            # n=2 is below the default terminal_kind_failures=3, so this follows
            # the ordinary backoff (threshold retry_base_minutes * 2**1 = 60 minutes)
            # instead of being suppressed outright.
            not_due = _seed_failed_resource(
                store,
                url=f"https://example.test/{kind}-not-due",
                failure_kind=kind,
                consecutive_failures=2,
                age=timedelta(minutes=1),
            )
            due = _seed_failed_resource(
                store,
                url=f"https://example.test/{kind}-due",
                failure_kind=kind,
                consecutive_failures=2,
                age=timedelta(minutes=61),
            )
            assert store.should_fetch_resource(not_due, refresh_days=30) is False, kind
            assert store.should_fetch_resource(due, refresh_days=30) is True, kind
    finally:
        store.close()


def test_should_fetch_resource_force_ignores_failure_kind(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        resource_id = _seed_failed_resource(
            store, url="https://example.test/dns-forced", failure_kind="dns", consecutive_failures=99
        )

        assert store.should_fetch_resource(resource_id, refresh_days=30, force=True) is True
    finally:
        store.close()


def test_should_fetch_resource_empty_terminal_failure_kinds_disables_mechanism(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        resource_id = _seed_failed_resource(
            store,
            url="https://example.test/dns-disabled",
            failure_kind="dns",
            consecutive_failures=3,
            age=timedelta(days=40),
        )

        assert store.should_fetch_resource(resource_id, refresh_days=30) is False
        # With the mechanism off, a DNS failure is just another backoff row: at
        # n=3 (threshold capped by retry_max_days) a 40-day-old failure is due.
        assert store.should_fetch_resource(resource_id, refresh_days=30, terminal_failure_kinds=()) is True
    finally:
        store.close()


def test_should_fetch_resource_terminal_failure_kinds_scoped_to_configured_kinds(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        resource_id = _seed_failed_resource(
            store,
            url="https://example.test/timeout-not-suppressed",
            failure_kind="timeout",
            consecutive_failures=3,
            age=timedelta(days=40),
        )

        assert store.should_fetch_resource(resource_id, refresh_days=30, terminal_failure_kinds=("dns",)) is True
    finally:
        store.close()


def test_terminal_failure_count_matches_should_fetch_resource_for_terminal_kind(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        terminal = _seed_failed_resource(
            store, url="https://example.test/dns-terminal", failure_kind="dns", consecutive_failures=3
        )
        _seed_failed_resource(
            store, url="https://example.test/dns-not-yet", failure_kind="dns", consecutive_failures=2
        )

        assert store.should_fetch_resource(terminal, refresh_days=30) is False
        assert store.terminal_failure_count(()) == 1
    finally:
        store.close()


def test_terminal_failure_count_counts_kind_condition_when_status_condition_is_empty(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        _seed_failed_resource(store, url="https://example.test/dns-only", failure_kind="dns", consecutive_failures=3)

        assert store.terminal_failure_count((), terminal_failure_kinds=("dns",)) == 1
    finally:
        store.close()


def test_terminal_failure_count_counts_status_condition_when_kind_condition_is_empty(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        _seed_failed_resource(store, url="https://example.test/gone", http_status=404)

        assert store.terminal_failure_count((404, 410), terminal_failure_kinds=()) == 1
    finally:
        store.close()


def test_terminal_failure_count_returns_zero_when_both_conditions_empty(tmp_path) -> None:
    store = VaultStore.open(tmp_path / "feedian.sqlite3")
    try:
        _seed_failed_resource(store, url="https://example.test/gone", http_status=404)
        _seed_failed_resource(
            store, url="https://example.test/dns", failure_kind="dns", consecutive_failures=3
        )

        assert store.terminal_failure_count((), terminal_failure_kinds=()) == 0
    finally:
        store.close()


def _downgrade_to_v8(path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            ALTER TABLE fetch_capture DROP COLUMN failure_kind;
            UPDATE schema_meta SET value = '8' WHERE key = 'schema_version';
            """
        )
        connection.commit()
    finally:
        connection.close()


def test_v8_migration_resets_high_consecutive_failures_to_one(tmp_path) -> None:
    path = tmp_path / "feedian.sqlite3"
    store = VaultStore.open(path)
    try:
        zero = _seed_failed_resource(store, url="https://example.test/n0", consecutive_failures=0)
        one = _seed_failed_resource(store, url="https://example.test/n1", consecutive_failures=1)
        two = _seed_failed_resource(store, url="https://example.test/n2", consecutive_failures=2)
        three = _seed_failed_resource(store, url="https://example.test/n3", consecutive_failures=3)
    finally:
        store.close()
    _downgrade_to_v8(path)

    migrated = VaultStore.open(path, allow_migration=True)
    try:
        assert migrated.schema_version() == 9
        rows = {
            resource_id: migrated.connection.execute(
                "SELECT consecutive_failures, failure_kind FROM fetch_capture WHERE resource_id = ?",
                (resource_id,),
            ).fetchone()
            for resource_id in (zero, one, two, three)
        }
        assert rows[zero]["consecutive_failures"] == 0
        assert rows[one]["consecutive_failures"] == 1
        assert rows[two]["consecutive_failures"] == 1
        assert rows[three]["consecutive_failures"] == 1
        assert all(row["failure_kind"] is None for row in rows.values())
    finally:
        migrated.close()


def test_v8_migration_keeps_terminal_http_status_suppression(tmp_path) -> None:
    path = tmp_path / "feedian.sqlite3"
    store = VaultStore.open(path)
    try:
        resource_id = _seed_failed_resource(
            store, url="https://example.test/gone", http_status=404, consecutive_failures=1
        )
    finally:
        store.close()
    _downgrade_to_v8(path)

    migrated = VaultStore.open(path, allow_migration=True)
    try:
        assert migrated.should_fetch_resource(resource_id, refresh_days=30) is False
    finally:
        migrated.close()


def test_v8_migration_does_not_touch_fetched_at(tmp_path) -> None:
    path = tmp_path / "feedian.sqlite3"
    store = VaultStore.open(path)
    try:
        resource_id = _seed_failed_resource(
            store, url="https://example.test/stale", consecutive_failures=2, age=timedelta(minutes=31)
        )
    finally:
        store.close()
    _downgrade_to_v8(path)

    migrated = VaultStore.open(path, allow_migration=True)
    try:
        # The migration reset consecutive_failures 2 -> 1 without touching
        # fetched_at, so the n=1 threshold (retry_base_minutes * 2**0 = 30
        # minutes) is already behind the pre-migration fetched_at: due at once.
        assert migrated.should_fetch_resource(resource_id, refresh_days=30) is True
    finally:
        migrated.close()


def test_v8_migration_row_needs_two_dns_failures_after_migration_to_become_terminal(tmp_path) -> None:
    path = tmp_path / "feedian.sqlite3"
    store = VaultStore.open(path)
    try:
        resource_id = _seed_failed_resource(store, url="https://example.test/dns-migrated", consecutive_failures=3)
    finally:
        store.close()
    _downgrade_to_v8(path)

    migrated = VaultStore.open(path, allow_migration=True)
    try:
        # The migration reset consecutive_failures 3 -> 1. One post-migration
        # DNS failure brings it to 2, still below terminal_kind_failures=3: an
        # aged row is due again through the ordinary backoff, not suppressed.
        migrated.record_failed_fetch(resource_id, warning="hostname could not be resolved", failure_kind="dns")
        assert (
            migrated.connection.execute(
                "SELECT consecutive_failures FROM fetch_capture WHERE resource_id = ?", (resource_id,)
            ).fetchone()["consecutive_failures"]
            == 2
        )
        migrated.connection.execute(
            "UPDATE fetch_capture SET fetched_at = ? WHERE resource_id = ?",
            ((datetime.now(timezone.utc) - timedelta(minutes=121)).isoformat(), resource_id),
        )
        migrated.connection.commit()
        assert migrated.should_fetch_resource(resource_id, refresh_days=30) is True

        # A second post-migration DNS failure reaches consecutive_failures=3:
        # now terminal, regardless of age.
        migrated.record_failed_fetch(resource_id, warning="hostname could not be resolved", failure_kind="dns")
        assert (
            migrated.connection.execute(
                "SELECT consecutive_failures FROM fetch_capture WHERE resource_id = ?", (resource_id,)
            ).fetchone()["consecutive_failures"]
            == 3
        )
        assert migrated.should_fetch_resource(resource_id, refresh_days=30) is False
    finally:
        migrated.close()


def test_a_migrated_database_has_the_same_fetch_capture_columns_as_a_fresh_one(tmp_path) -> None:
    """Same schema_version must mean the same table, however it got there."""

    def fetch_capture_columns(path) -> dict[str, tuple]:
        store = VaultStore.open(path, allow_migration=True)
        try:
            assert store.schema_version() == 9
            rows = store.connection.execute("PRAGMA table_info(fetch_capture)").fetchall()
            return {row[1]: (row[2], row[3], row[5]) for row in rows}
        finally:
            store.close()

    fresh_path = tmp_path / "fresh.sqlite3"
    fresh_columns = fetch_capture_columns(fresh_path)

    migrated_path = tmp_path / "migrated.sqlite3"
    VaultStore.open(migrated_path).close()
    _downgrade_to_v8(migrated_path)

    assert fetch_capture_columns(migrated_path) == fresh_columns
