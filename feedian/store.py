from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .canonical import CanonicalItem, canonicalize_url
from .ids import uuid7


SCHEMA_VERSION = 6


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _header_value(headers: dict[str, str] | None, name: str) -> str:
    if not headers:
        return ""
    wanted = name.lower()
    return next((str(value) for key, value in headers.items() if str(key).lower() == wanted), "")


def _comment_content_hash(
    *, body: str, tags_json: str, star_count: int | None, posted_at: str, metadata_json: str
) -> str:
    return sha256_bytes(
        stable_json(
            {
                "body": body,
                "tags": tags_json,
                "star_count": star_count,
                "posted_at": posted_at,
                "metadata": metadata_json,
            }
        ).encode("utf-8")
    )


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a write batch atomically.

    Schema migrations use this as well as ordinary writes, so a migration that
    fails part way through leaves the database at its previous schema version
    rather than half upgraded.
    """
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


@dataclass(frozen=True)
class StoredItem:
    source_item_id: str
    resource_id: str | None
    source_revision_id: str
    changed: bool


class VaultStore:
    """The local, per-vault canonical store. Markdown is rendered from this database."""

    def __init__(self, connection: sqlite3.Connection, path: Path) -> None:
        self.connection = connection
        self.path = path

    @classmethod
    def open(cls, path: str | Path, *, allow_migration: bool = False) -> "VaultStore":
        database_path = Path(path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        store = cls(connection, database_path)
        try:
            store.migrate(allow_migration=allow_migration)
        except BaseException:
            connection.close()
            raise
        return store

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with _transaction(self.connection) as connection:
            yield connection

    def migrate(self, *, allow_migration: bool = False) -> None:
        self.connection.execute("CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        row = self.connection.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
        current = int(row["value"]) if row is not None else 0
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"Vault database schema {current} is newer than this Feedian version ({SCHEMA_VERSION})."
            )
        if current == 0:
            with self.transaction() as connection:
                _create_schema(connection)
                connection.execute(
                    "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
                connection.execute("INSERT INTO schema_meta(key, value) VALUES ('vault_id', ?)", (uuid7(),))
                connection.execute("INSERT INTO schema_meta(key, value) VALUES ('created_at', ?)", (utc_now(),))
                connection.execute("INSERT INTO schema_meta(key, value) VALUES ('search_generation', '0')")
        elif current < SCHEMA_VERSION:
            if not allow_migration:
                raise RuntimeError("Database migration is required; run `feedian migrate --vault ...`.")
            self._migrate_schema(current)

    def _migrate_schema(self, current: int) -> None:
        while current < SCHEMA_VERSION:
            if current == 1:
                _migrate_v1_to_v2(self.connection)
                current = 2
                continue
            if current == 2:
                _migrate_v2_to_v3(self.connection)
                current = 3
                continue
            if current == 3:
                _migrate_v3_to_v4(self.connection)
                current = 4
                continue
            if current == 4:
                _migrate_v4_to_v5(self.connection)
                current = 5
                continue
            if current == 5:
                _migrate_v5_to_v6(self.connection)
                current = 6
                continue
            raise RuntimeError(f"No migration path from database schema {current}.")

    def compact(self) -> None:
        self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.connection.execute("VACUUM")

    def search_generation(self) -> int:
        row = self.connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'search_generation'"
        ).fetchone()
        return int(row["value"]) if row is not None else 0

    @staticmethod
    def _mark_search_dirty(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            INSERT INTO schema_meta(key, value) VALUES ('search_generation', '1')
            ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)
            """
        )

    def schema_version(self) -> int:
        row = self.connection.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
        return int(row["value"])

    def vault_id(self) -> str:
        row = self.connection.execute("SELECT value FROM schema_meta WHERE key = 'vault_id'").fetchone()
        return str(row["value"])

    def quick_check(self) -> str:
        return str(self.connection.execute("PRAGMA quick_check").fetchone()[0])

    def integrity_check(self) -> str:
        return str(self.connection.execute("PRAGMA integrity_check").fetchone()[0])

    def put_payload(
        self,
        content: bytes,
        *,
        media_type: str = "application/octet-stream",
        charset: str = "",
        source_url: str = "",
        headers: dict[str, str] | None = None,
    ) -> str:
        with self.transaction() as connection:
            return self._put_payload(
                connection,
                content,
                media_type=media_type,
                charset=charset,
                source_url=source_url,
                headers=headers,
            )

    def _put_payload(
        self,
        connection: sqlite3.Connection,
        content: bytes,
        *,
        media_type: str,
        charset: str = "",
        source_url: str = "",
        headers: dict[str, str] | None = None,
    ) -> str:
        digest = sha256_bytes(content)
        row = connection.execute("SELECT payload_id FROM payload WHERE sha256 = ?", (digest,)).fetchone()
        if row is not None:
            return str(row["payload_id"])
        payload_id = uuid7()
        connection.execute(
            """
            INSERT INTO payload(payload_id, sha256, content, media_type, charset, source_url, headers_json, byte_length, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload_id,
                digest,
                content,
                media_type,
                charset,
                source_url,
                stable_json(headers or {}),
                len(content),
                utc_now(),
            ),
        )
        return payload_id

    def upsert_canonical_item(
        self,
        item: CanonicalItem,
        *,
        account: str = "default",
        source_payload: bytes | None = None,
        source_media_type: str = "application/json",
    ) -> StoredItem:
        metadata = item.as_bookmark_metadata()
        metadata_json = stable_json(metadata)
        metadata_hash = sha256_bytes(metadata_json.encode("utf-8"))
        now = utc_now()
        with self.transaction() as connection:
            resource_id = self._resolve_resource(connection, item, now)
            source_row = connection.execute(
                """
                SELECT source_item_id, current_revision_id FROM source_item
                WHERE provider = ? AND account = ? AND native_id = ?
                """,
                (item.source, account, item.source_id),
            ).fetchone()
            if source_row is None:
                source_item_id = uuid7()
                connection.execute(
                    """
                    INSERT INTO source_item(source_item_id, provider, account, native_id, resource_id, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (source_item_id, item.source, account, item.source_id, resource_id, now, now),
                )
                previous_hash = None
            else:
                source_item_id = str(source_row["source_item_id"])
                previous_hash_row = connection.execute(
                    """
                    SELECT sr.metadata_hash, p.sha256 AS payload_sha256
                    FROM source_item_revision AS sr
                    LEFT JOIN payload AS p ON p.payload_id = sr.payload_id
                    WHERE sr.source_revision_id = ?
                    """,
                    (source_row["current_revision_id"],),
                ).fetchone()
                previous_hash = str(previous_hash_row["metadata_hash"]) if previous_hash_row else None
                previous_payload_hash = (
                    str(previous_hash_row["payload_sha256"])
                    if previous_hash_row is not None and previous_hash_row["payload_sha256"]
                    else None
                )
                connection.execute(
                    "UPDATE source_item SET resource_id = ?, updated_at = ?, removed_at = NULL WHERE source_item_id = ?",
                    (resource_id, now, source_item_id),
                )
            if source_row is None:
                previous_payload_hash = None
            payload_hash = sha256_bytes(source_payload) if source_payload is not None else previous_payload_hash
            changed = metadata_hash != previous_hash or payload_hash != previous_payload_hash
            if changed:
                payload_id = (
                    self._put_payload(connection, source_payload, media_type=source_media_type)
                    if source_payload is not None
                    else None
                )
                if source_row is None or not source_row["current_revision_id"]:
                    source_revision_id = uuid7()
                    connection.execute(
                        """
                        INSERT INTO source_item_revision(
                            source_revision_id, source_item_id, metadata_json, metadata_hash, payload_id, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (source_revision_id, source_item_id, metadata_json, metadata_hash, payload_id, now),
                    )
                    connection.execute(
                        "UPDATE source_item SET current_revision_id = ? WHERE source_item_id = ?",
                        (source_revision_id, source_item_id),
                    )
                else:
                    source_revision_id = str(source_row["current_revision_id"])
                    connection.execute(
                        """
                        UPDATE source_item_revision
                        SET metadata_json = ?, metadata_hash = ?, payload_id = ?, created_at = ?
                        WHERE source_revision_id = ?
                        """,
                        (metadata_json, metadata_hash, payload_id, now, source_revision_id),
                    )
                self._mark_search_dirty(connection)
            else:
                source_revision_id = str(source_row["current_revision_id"])
        if changed:
            self.delete_orphan_payloads()
        return StoredItem(source_item_id, resource_id, source_revision_id, changed)

    def record_resource_revision(
        self,
        resource_id: str,
        *,
        content_markdown: str,
        title: str = "",
        final_url: str = "",
        extracted_by: str = "",
        http_payload_id: str | None = None,
        rendered_payload_id: str | None = None,
        discussion_text: str = "",
        content_truncated: bool = False,
        warning: str | None = None,
        response_headers: dict[str, str] | None = None,
    ) -> tuple[str, bool]:
        content_hash = sha256_bytes(
            stable_json(
                {"title": title, "content_markdown": content_markdown, "discussion_text": discussion_text}
            ).encode("utf-8")
        )
        now = utc_now()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT current_revision_id FROM resource WHERE resource_id = ?", (resource_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown resource: {resource_id}")
            current_id = row["current_revision_id"]
            current_hash_row = (
                connection.execute("SELECT content_hash FROM resource_revision WHERE resource_revision_id = ?", (current_id,)).fetchone()
                if current_id
                else None
            )
            changed = current_hash_row is None or current_hash_row["content_hash"] != content_hash
            if current_id:
                revision_id = str(current_id)
                if changed:
                    connection.execute(
                        """
                        UPDATE resource_revision
                        SET title = ?, content_markdown = ?, discussion_text = ?, content_hash = ?, created_at = ?
                        WHERE resource_revision_id = ?
                        """,
                        (title, content_markdown, discussion_text, content_hash, now, revision_id),
                    )
            else:
                revision_id = uuid7()
                connection.execute(
                    """
                    INSERT INTO resource_revision(resource_revision_id, resource_id, title, content_markdown,
                                                  discussion_text, content_hash, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (revision_id, resource_id, title, content_markdown, discussion_text, content_hash, now),
                )
                changed = True
            connection.execute(
                "UPDATE resource SET current_revision_id = ?, updated_at = ? WHERE resource_id = ?",
                (revision_id, now, resource_id),
            )
            capture = connection.execute(
                "SELECT fetch_capture_id FROM fetch_capture WHERE resource_id = ? ORDER BY fetched_at DESC LIMIT 1",
                (resource_id,),
            ).fetchone()
            if capture is None:
                connection.execute(
                    """
                    INSERT INTO fetch_capture(fetch_capture_id, resource_id, resource_revision_id, http_payload_id,
                                              rendered_payload_id, final_url, extracted_by, content_truncated,
                                              warning, response_etag, response_last_modified, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (uuid7(), resource_id, revision_id, http_payload_id, rendered_payload_id, final_url,
                     extracted_by, int(content_truncated), warning,
                     _header_value(response_headers, "ETag"), _header_value(response_headers, "Last-Modified"), now),
                )
            else:
                connection.execute(
                    """
                    UPDATE fetch_capture
                    SET resource_revision_id = ?, http_payload_id = ?, rendered_payload_id = ?, final_url = ?,
                        extracted_by = ?, content_truncated = ?, warning = ?, response_etag = ?,
                        response_last_modified = ?, fetched_at = ?
                    WHERE fetch_capture_id = ?
                    """,
                    (revision_id, http_payload_id, rendered_payload_id, final_url, extracted_by,
                     int(content_truncated), warning, _header_value(response_headers, "ETag"),
                     _header_value(response_headers, "Last-Modified"), now, capture["fetch_capture_id"]),
                )
            if changed:
                self._mark_search_dirty(connection)
        self.delete_orphan_payloads()
        return revision_id, changed

    def upsert_comment(
        self,
        *,
        provider: str,
        resource_id: str,
        author: str,
        body: str,
        tags: list[str] | None = None,
        star_count: int | None = None,
        posted_at: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> tuple[str, bool]:
        results = self.upsert_comments(
            provider=provider,
            resource_id=resource_id,
            comments=[
                {
                    "author": author,
                    "body": body,
                    "tags": tags or [],
                    "star_count": star_count,
                    "posted_at": posted_at,
                    "metadata": metadata or {},
                }
            ],
        )
        return results[0]

    def upsert_comments(
        self,
        *,
        provider: str,
        resource_id: str,
        comments: list[dict[str, Any]],
        replace: bool = False,
    ) -> list[tuple[str, bool]]:
        """Upsert one resource's comments in a single durable transaction."""
        if any(not str(comment.get("author") or "") for comment in comments):
            raise ValueError("Comment author must not be empty.")

        existing_rows = self.connection.execute(
            """
            SELECT c.comment_id, c.author, c.removed_at, cr.star_count, cr.content_hash
            FROM comment AS c
            LEFT JOIN comment_revision AS cr ON cr.comment_revision_id = c.current_revision_id
            WHERE c.provider = ? AND c.resource_id = ?
            """,
            (provider, resource_id),
        ).fetchall()
        existing_by_author = {str(row["author"]): row for row in existing_rows}
        results: list[tuple[str, bool] | None] = [None] * len(comments)
        pending: list[tuple[int, str, str, str, int | None, str, str, str, sqlite3.Row | None]] = []

        for index, comment in enumerate(comments):
            author = str(comment.get("author") or "")
            body = str(comment.get("body") or "")
            tags = [str(tag) for tag in (comment.get("tags") or [])]
            posted_at = str(comment.get("posted_at") or "")
            metadata = dict(comment.get("metadata") or {})
            existing = existing_by_author.get(author)
            star_count = comment.get("star_count")
            if star_count is None and existing is not None and existing["star_count"] is not None:
                star_count = int(existing["star_count"])
            elif star_count is not None:
                star_count = int(star_count)
            tags_json = stable_json(tags)
            metadata_json = stable_json(metadata)
            content_hash = _comment_content_hash(
                body=body,
                tags_json=tags_json,
                star_count=star_count,
                posted_at=posted_at,
                metadata_json=metadata_json,
            )
            if (
                existing is not None
                and existing["removed_at"] is None
                and existing["content_hash"] == content_hash
            ):
                results[index] = (str(existing["comment_id"]), False)
                continue
            pending.append(
                (index, author, body, tags_json, star_count, posted_at, metadata_json, content_hash, existing)
            )

        if not pending and not replace:
            return [result for result in results if result is not None]

        now = utc_now()
        any_changed = False
        with self.transaction() as connection:
            for index, author, body, tags_json, star_count, posted_at, metadata_json, content_hash, existing in pending:
                if existing is None:
                    comment_id = uuid7()
                    current_hash = None
                    connection.execute(
                        """
                        INSERT INTO comment(comment_id, provider, resource_id, author, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (comment_id, provider, resource_id, author, now, now),
                    )
                else:
                    comment_id = str(existing["comment_id"])
                    current_hash = str(existing["content_hash"]) if existing["content_hash"] else None
                    connection.execute(
                        "UPDATE comment SET updated_at = ?, removed_at = NULL WHERE comment_id = ?",
                        (now, comment_id),
                    )
                changed = current_hash != content_hash
                if changed:
                    if existing is not None and existing["comment_id"]:
                        revision_row = connection.execute(
                            "SELECT current_revision_id FROM comment WHERE comment_id = ?", (comment_id,)
                        ).fetchone()
                    else:
                        revision_row = None
                    current_revision_id = revision_row["current_revision_id"] if revision_row else None
                    if current_revision_id:
                        connection.execute(
                            """
                            UPDATE comment_revision
                            SET body = ?, tags_json = ?, star_count = ?, posted_at = ?, metadata_json = ?,
                                content_hash = ?, created_at = ?
                            WHERE comment_revision_id = ?
                            """,
                            (body, tags_json, star_count, posted_at, metadata_json, content_hash, now, current_revision_id),
                        )
                    else:
                        revision_id = uuid7()
                        connection.execute(
                            """
                            INSERT INTO comment_revision(comment_revision_id, comment_id, body, tags_json, star_count,
                                                         posted_at, metadata_json, content_hash, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (revision_id, comment_id, body, tags_json, star_count, posted_at, metadata_json, content_hash, now),
                        )
                        connection.execute(
                            "UPDATE comment SET current_revision_id = ? WHERE comment_id = ?",
                            (revision_id, comment_id),
                        )
                    any_changed = True
                results[index] = (comment_id, changed)
            if replace:
                keep_authors = {str(comment.get("author") or "") for comment in comments}
                stale_ids = [
                    str(row["comment_id"])
                    for row in existing_rows
                    if str(row["author"]) not in keep_authors
                ]
                if stale_ids:
                    connection.executemany(
                        "DELETE FROM comment_revision WHERE comment_id = ?",
                        [(comment_id,) for comment_id in stale_ids],
                    )
                    connection.executemany(
                        "DELETE FROM comment WHERE comment_id = ?",
                        [(comment_id,) for comment_id in stale_ids],
                    )
                    any_changed = True
            if any_changed:
                self._mark_search_dirty(connection)
        return [result for result in results if result is not None]

    def create_sync_run(self, providers: list[str], settings_fingerprint: str) -> str:
        run_id = uuid7()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO sync_run(sync_run_id, providers_json, settings_fingerprint, status, started_at)
                VALUES (?, ?, ?, 'running', ?)
                """,
                (run_id, stable_json(providers), settings_fingerprint, utc_now()),
            )
        return run_id

    def fail_interrupted_sync_runs(self) -> int:
        """Close runs left active by a terminated process before starting again."""
        now = utc_now()
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE sync_run
                SET status = 'failed', error = coalesce(error, 'interrupted before the next sync'), finished_at = ?
                WHERE status = 'running'
                """,
                (now,),
            )
        return int(cursor.rowcount)

    def finish_sync_run(self, run_id: str, *, status: str, error: str | None = None) -> None:
        if status not in {"completed", "failed", "partial"}:
            raise ValueError(f"Unsupported sync run status: {status}")
        with self.transaction() as connection:
            connection.execute(
                "UPDATE sync_run SET status = ?, error = ?, finished_at = ? WHERE sync_run_id = ?",
                (status, error, utc_now(), run_id),
            )

    def record_sync_item(self, run_id: str, source_item_id: str, status: str, error: str | None = None) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO sync_run_item(sync_run_id, source_item_id, status, error, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(sync_run_id, source_item_id) DO UPDATE SET
                  status = excluded.status, error = excluded.error, updated_at = excluded.updated_at
                """,
                (run_id, source_item_id, status, error, utc_now()),
            )

    def status_counts(self) -> dict[str, int]:
        tables = (
            "resource", "resource_revision", "source_item", "source_item_revision", "comment",
            "resource_image", "payload",
            "llm_run", "source_note", "snapshot",
        )
        return {table: int(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in tables}

    def latest_sync_run(self) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM sync_run ORDER BY started_at DESC LIMIT 1").fetchone()

    def latest_provider_sync_run(self, provider: str) -> sqlite3.Row | None:
        return self.connection.execute(
            """
            SELECT * FROM sync_run
            WHERE status = 'completed' AND providers_json LIKE ?
            ORDER BY finished_at DESC LIMIT 1
            """,
            (f'%"{provider}"%',),
        ).fetchone()

    def should_fetch_resource(self, resource_id: str, *, refresh_days: int, force: bool = False) -> bool:
        if force:
            return True
        latest = self.connection.execute(
            """
            SELECT fc.fetched_at, fc.warning, fc.http_payload_id, fc.rendered_payload_id,
                   length(trim(coalesce(rr.content_markdown, ''))) AS content_length
            FROM fetch_capture AS fc
            LEFT JOIN resource_revision AS rr ON rr.resource_revision_id = fc.resource_revision_id
            WHERE fc.resource_id = ? ORDER BY fc.fetched_at DESC LIMIT 1
            """,
            (resource_id,),
        ).fetchone()
        if latest is None:
            return True
        fetched_at = datetime.fromisoformat(str(latest["fetched_at"]).replace("Z", "+00:00"))
        if (
            latest["warning"]
            and not latest["http_payload_id"]
            and not latest["rendered_payload_id"]
            and int(latest["content_length"] or 0) == 0
        ):
            return datetime.now(timezone.utc) - fetched_at >= timedelta(minutes=30)
        return datetime.now(timezone.utc) - fetched_at >= timedelta(days=max(1, refresh_days))

    def resource_fetch_validators(self, resource_id: str) -> tuple[str, str]:
        row = self.connection.execute(
            """
            SELECT response_etag, response_last_modified
            FROM fetch_capture
            WHERE resource_id = ?
            ORDER BY fetched_at DESC LIMIT 1
            """,
            (resource_id,),
        ).fetchone()
        if row is None:
            return "", ""
        return str(row["response_etag"] or ""), str(row["response_last_modified"] or "")

    def record_not_modified_fetch(
        self, resource_id: str, *, final_url: str, response_headers: dict[str, str] | None = None
    ) -> None:
        with self.transaction() as connection:
            capture = connection.execute(
                "SELECT fetch_capture_id FROM fetch_capture WHERE resource_id = ? ORDER BY fetched_at DESC LIMIT 1",
                (resource_id,),
            ).fetchone()
            if capture is None:
                return
            connection.execute(
                """
                UPDATE fetch_capture
                SET final_url = ?, response_etag = COALESCE(?, response_etag),
                    response_last_modified = COALESCE(?, response_last_modified), fetched_at = ?
                WHERE fetch_capture_id = ?
                """,
                (final_url, _header_value(response_headers, "ETag") or None,
                 _header_value(response_headers, "Last-Modified") or None, utc_now(), capture["fetch_capture_id"]),
            )

    def backup_to(self, destination: str | Path) -> None:
        """Create a consistent SQLite backup without copying WAL files by hand."""
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        destination_connection = sqlite3.connect(target)
        try:
            self.connection.backup(destination_connection)
            if str(destination_connection.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
                raise RuntimeError("SQLite backup integrity check failed.")
        finally:
            destination_connection.close()

    def record_snapshot(self, snapshot_id: str, manifest: dict[str, Any]) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO snapshot(snapshot_id, manifest_json, created_at) VALUES (?, ?, ?)",
                (snapshot_id, stable_json(manifest), utc_now()),
            )

    def mark_snapshot_verified(self, snapshot_id: str) -> None:
        with self.transaction() as connection:
            connection.execute("UPDATE snapshot SET verified_at = ? WHERE snapshot_id = ?", (utc_now(), snapshot_id))

    def latest_snapshot(self) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM snapshot ORDER BY created_at DESC LIMIT 1").fetchone()

    def successful_llm_result(
        self,
        *,
        resource_revision_id: str,
        operation: str,
        model: str,
        prompt_version: str,
        input_fingerprint: str,
        backend: str = "openai-responses",
        summary_schema_version: str = "1",
        legacy_fingerprint: str | None = None,
    ) -> dict[str, Any] | None:
        """Find a reusable result, reading only.

        `plan_source_notes` calls this outside the vault write lock and `--dry-run`
        promises no writes, so a version-one hit is left as it is here;
        `promote_legacy_fingerprint` rewrites it from the write path instead.
        """
        row = self._completed_llm_run(
            resource_revision_id=resource_revision_id, operation=operation, backend=backend,
            model=model, prompt_version=prompt_version, summary_schema_version=summary_schema_version,
            fingerprint=input_fingerprint, fingerprint_version=2,
        )
        if row is None and legacy_fingerprint:
            row = self._completed_llm_run(
                resource_revision_id=resource_revision_id, operation=operation, backend=backend,
                model=model, prompt_version=prompt_version, summary_schema_version=summary_schema_version,
                fingerprint=legacy_fingerprint, fingerprint_version=1,
            )
        return json.loads(str(row["result_json"])) if row is not None and row["result_json"] else None

    def promote_legacy_fingerprint(
        self,
        *,
        resource_revision_id: str,
        operation: str,
        model: str,
        prompt_version: str,
        input_fingerprint: str,
        legacy_fingerprint: str,
        backend: str = "openai-responses",
        summary_schema_version: str = "1",
    ) -> bool:
        """Rewrite a version-one reuse key to the current one. Callers hold the write lock."""
        row = self._completed_llm_run(
            resource_revision_id=resource_revision_id, operation=operation, backend=backend,
            model=model, prompt_version=prompt_version, summary_schema_version=summary_schema_version,
            fingerprint=legacy_fingerprint, fingerprint_version=1,
        )
        if row is None:
            return False
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE llm_run
                SET input_fingerprint = ?,
                    fingerprint_version = 2,
                    backend_metadata_json = json_set(
                        COALESCE(backend_metadata_json, '{}'),
                        '$.legacy_fingerprint_promoted', json('true')
                    )
                WHERE llm_run_id = ?
                """,
                (input_fingerprint, str(row["llm_run_id"])),
            )
        return True

    def _completed_llm_run(
        self,
        *,
        resource_revision_id: str,
        operation: str,
        backend: str,
        model: str,
        prompt_version: str,
        summary_schema_version: str,
        fingerprint: str,
        fingerprint_version: int,
    ) -> Any:
        return self.connection.execute(
            """
            SELECT llm_run_id, result_json FROM llm_run
            WHERE resource_revision_id = ? AND operation = ? AND backend = ? AND model = ?
              AND prompt_version = ? AND summary_schema_version = ? AND fingerprint_version = ?
              AND input_fingerprint = ? AND status = 'completed'
            ORDER BY finished_at DESC LIMIT 1
            """,
            (
                resource_revision_id, operation, backend, model, prompt_version,
                summary_schema_version, fingerprint_version, fingerprint,
            ),
        ).fetchone()

    def start_llm_run(
        self,
        *,
        resource_id: str,
        resource_revision_id: str,
        operation: str,
        model: str,
        prompt_version: str,
        input_fingerprint: str,
        request: dict[str, Any],
        backend: str = "openai-responses",
        summary_schema_version: str = "1",
        fingerprint_version: int = 2,
        auth_mode: str = "unknown",
        billing_mode: str = "unknown",
        backend_metadata: dict[str, Any] | None = None,
    ) -> str:
        run_id = uuid7()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO llm_run(
                    llm_run_id, resource_id, resource_revision_id, operation, backend, model, prompt_version,
                    summary_schema_version, fingerprint_version, input_fingerprint, request_json,
                    auth_mode, billing_mode, backend_metadata_json, status, started_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?)
                """,
                (
                    run_id, resource_id, resource_revision_id, operation, backend, model, prompt_version,
                    summary_schema_version, fingerprint_version, input_fingerprint, stable_json(request),
                    auth_mode, billing_mode, stable_json(backend_metadata or {}), utc_now(),
                ),
            )
        return run_id

    def finish_llm_run(
        self,
        run_id: str,
        *,
        request: dict[str, Any] | None = None,
        response: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        usage: dict[str, Any] | None = None,
        price: dict[str, Any] | None = None,
        error: str | None = None,
        auth_mode: str | None = None,
        billing_mode: str | None = None,
        backend_metadata: dict[str, Any] | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """Close a run. `request` records what was actually sent, which can differ
        in shape from the planned request the run started with (see llm providers)."""
        status = "failed" if error else "completed"
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE llm_run SET request_json = COALESCE(?, request_json), response_json = ?, result_json = ?,
                                   usage_json = ?, price_json = ?, status = ?, error = ?, finished_at = ?
                                   , auth_mode = COALESCE(?, auth_mode)
                                   , billing_mode = COALESCE(?, billing_mode)
                                   , backend_metadata_json = COALESCE(?, backend_metadata_json)
                                   , duration_ms = ?
                WHERE llm_run_id = ?
                """,
                (stable_json(request) if request is not None else None,
                 stable_json(response) if response is not None else None, stable_json(result) if result is not None else None,
                 stable_json(usage) if usage is not None else None, stable_json(price) if price is not None else None,
                 status, error, utc_now(), auth_mode, billing_mode,
                 stable_json(backend_metadata) if backend_metadata is not None else None,
                 duration_ms, run_id),
            )

    def put_source_note(self, *, resource_id: str, llm_run_id: str | None, markdown: str) -> str:
        markdown_hash = sha256_bytes(markdown.encode("utf-8"))
        current = self.connection.execute(
            "SELECT source_note_id, markdown_hash FROM source_note WHERE resource_id = ? AND superseded_at IS NULL",
            (resource_id,),
        ).fetchone()
        if current is not None and current["markdown_hash"] == markdown_hash:
            return str(current["source_note_id"])
        now = utc_now()
        with self.transaction() as connection:
            if current is not None:
                note_id = str(current["source_note_id"])
                connection.execute(
                    """
                    UPDATE source_note
                    SET llm_run_id = ?, markdown = ?, markdown_hash = ?, created_at = ?, superseded_at = NULL
                    WHERE source_note_id = ?
                    """,
                    (llm_run_id, markdown, markdown_hash, now, note_id),
                )
            else:
                note_id = uuid7()
                connection.execute(
                    """
                    INSERT INTO source_note(source_note_id, resource_id, llm_run_id, markdown, markdown_hash, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (note_id, resource_id, llm_run_id, markdown, markdown_hash, now),
                )
        return note_id

    def replace_resource_images(
        self,
        *,
        resource_id: str,
        resource_revision_id: str,
        images: list[tuple[str, str]],
    ) -> int:
        unique_images: list[tuple[str, str]] = []
        seen_urls: set[str] = set()
        for url, alt in images:
            normalized_url = url.strip()
            if not normalized_url or normalized_url in seen_urls:
                continue
            seen_urls.add(normalized_url)
            unique_images.append((normalized_url, alt.strip()))
        with self.transaction() as connection:
            connection.execute("DELETE FROM resource_image WHERE resource_id = ?", (resource_id,))
            now = utc_now()
            connection.executemany(
                """
                INSERT INTO resource_image(resource_image_id, resource_id, resource_revision_id, source_url,
                                           alt_text, position, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (uuid7(), resource_id, resource_revision_id, url, alt, position, now)
                    for position, (url, alt) in enumerate(unique_images)
                ],
            )
        return len(unique_images)

    def comment_bookmark_count(self, resource_id: str, *, provider: str = "hatena") -> int | None:
        row = self.connection.execute(
            "SELECT bookmark_count FROM resource_comment_state WHERE provider = ? AND resource_id = ?",
            (provider, resource_id),
        ).fetchone()
        return int(row["bookmark_count"]) if row is not None else None

    def update_comment_state(
        self,
        resource_id: str,
        bookmark_count: int,
        *,
        provider: str = "hatena",
        entry_url: str = "",
        entry_id: str = "",
    ) -> None:
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO resource_comment_state(
                    provider, resource_id, bookmark_count, entry_url, entry_id, checked_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, resource_id) DO UPDATE SET
                    bookmark_count = excluded.bookmark_count,
                    entry_url = excluded.entry_url,
                    entry_id = excluded.entry_id,
                    checked_at = excluded.checked_at,
                    updated_at = excluded.updated_at
                """,
                (provider, resource_id, max(0, int(bookmark_count)), entry_url, entry_id, now, now),
            )

    def update_comment_star_counts(
        self, updates: dict[str, int | None], *, checked_at: str | None = None
    ) -> int:
        if not updates:
            return 0
        checked = checked_at or utc_now()
        changed = 0
        with self.transaction() as connection:
            for comment_id, star_count in updates.items():
                row = connection.execute(
                    """
                    SELECT cr.comment_revision_id, cr.body, cr.tags_json, cr.star_count,
                           cr.posted_at, cr.metadata_json
                    FROM comment AS c
                    JOIN comment_revision AS cr ON cr.comment_revision_id = c.current_revision_id
                    WHERE c.comment_id = ?
                    """,
                    (comment_id,),
                ).fetchone()
                if row is None:
                    continue
                if star_count is None:
                    connection.execute(
                        "UPDATE comment_revision SET star_checked_at = ? WHERE comment_revision_id = ?",
                        (checked, row["comment_revision_id"]),
                    )
                    continue
                normalized_count = max(0, int(star_count))
                content_hash = _comment_content_hash(
                    body=str(row["body"]),
                    tags_json=str(row["tags_json"]),
                    star_count=normalized_count,
                    posted_at=str(row["posted_at"]),
                    metadata_json=str(row["metadata_json"]),
                )
                count_changed = row["star_count"] is None or int(row["star_count"]) != normalized_count
                connection.execute(
                    """
                    UPDATE comment_revision
                    SET star_count = ?, star_checked_at = ?, content_hash = ?,
                        created_at = CASE WHEN ? THEN ? ELSE created_at END
                    WHERE comment_revision_id = ?
                    """,
                    (normalized_count, checked, content_hash, int(count_changed), checked, row["comment_revision_id"]),
                )
                changed += int(count_changed)
        return changed

    def delete_orphan_payloads(self) -> int:
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                DELETE FROM payload
                WHERE payload_id NOT IN (
                    SELECT payload_id FROM source_item_revision WHERE payload_id IS NOT NULL
                    UNION
                    SELECT http_payload_id FROM fetch_capture WHERE http_payload_id IS NOT NULL
                    UNION
                    SELECT rendered_payload_id FROM fetch_capture WHERE rendered_payload_id IS NOT NULL
                    UNION
                    SELECT payload_id FROM asset WHERE payload_id IS NOT NULL
                )
                """
            )
        return int(cursor.rowcount)

    def _resolve_resource(self, connection: sqlite3.Connection, item: CanonicalItem, now: str) -> str | None:
        if not item.url:
            resource_id = uuid7()
            connection.execute(
                "INSERT INTO resource(resource_id, kind, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (resource_id, item.item_type or "item", now, now),
            )
            connection.execute(
                "INSERT INTO resource_identifier(identifier_id, resource_id, namespace, value, is_primary, created_at) VALUES (?, ?, ?, ?, 1, ?)",
                (uuid7(), resource_id, f"{item.source}_native", item.source_id, now),
            )
            return resource_id
        normalized_url = canonicalize_url(item.url)
        row = connection.execute(
            "SELECT resource_id FROM resource_identifier WHERE namespace = 'url' AND value = ?", (normalized_url,)
        ).fetchone()
        if row is not None:
            return str(row["resource_id"])
        resource_id = uuid7()
        connection.execute(
            "INSERT INTO resource(resource_id, kind, created_at, updated_at) VALUES (?, 'web', ?, ?)",
            (resource_id, now, now),
        )
        connection.execute(
            "INSERT INTO resource_identifier(identifier_id, resource_id, namespace, value, is_primary, created_at) VALUES (?, ?, 'url', ?, 1, ?)",
            (uuid7(), resource_id, normalized_url, now),
        )
        return resource_id

def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS payload (
            payload_id TEXT PRIMARY KEY,
            sha256 TEXT NOT NULL UNIQUE,
            content BLOB NOT NULL,
            media_type TEXT NOT NULL,
            charset TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '',
            headers_json TEXT NOT NULL DEFAULT '{}',
            byte_length INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS resource (
            resource_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            current_revision_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            removed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS resource_identifier (
            identifier_id TEXT PRIMARY KEY,
            resource_id TEXT NOT NULL REFERENCES resource(resource_id),
            namespace TEXT NOT NULL,
            value TEXT NOT NULL,
            is_primary INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            UNIQUE(namespace, value)
        );
        CREATE TABLE IF NOT EXISTS source_item (
            source_item_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            account TEXT NOT NULL,
            native_id TEXT NOT NULL,
            resource_id TEXT REFERENCES resource(resource_id),
            current_revision_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            removed_at TEXT,
            UNIQUE(provider, account, native_id)
        );
        CREATE TABLE IF NOT EXISTS source_item_revision (
            source_revision_id TEXT PRIMARY KEY,
            source_item_id TEXT NOT NULL REFERENCES source_item(source_item_id),
            metadata_json TEXT NOT NULL,
            metadata_hash TEXT NOT NULL,
            payload_id TEXT REFERENCES payload(payload_id),
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS resource_revision (
            resource_revision_id TEXT PRIMARY KEY,
            resource_id TEXT NOT NULL REFERENCES resource(resource_id),
            title TEXT NOT NULL DEFAULT '',
            content_markdown TEXT NOT NULL,
            discussion_text TEXT NOT NULL DEFAULT '',
            content_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fetch_capture (
            fetch_capture_id TEXT PRIMARY KEY,
            resource_id TEXT NOT NULL REFERENCES resource(resource_id),
            resource_revision_id TEXT REFERENCES resource_revision(resource_revision_id),
            http_payload_id TEXT REFERENCES payload(payload_id),
            rendered_payload_id TEXT REFERENCES payload(payload_id),
            final_url TEXT NOT NULL DEFAULT '',
            extracted_by TEXT NOT NULL DEFAULT '',
            content_truncated INTEGER NOT NULL DEFAULT 0,
            warning TEXT,
            response_etag TEXT NOT NULL DEFAULT '',
            response_last_modified TEXT NOT NULL DEFAULT '',
            fetched_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS comment (
            comment_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            resource_id TEXT NOT NULL REFERENCES resource(resource_id),
            author TEXT NOT NULL,
            current_revision_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            removed_at TEXT,
            UNIQUE(provider, resource_id, author)
        );
        CREATE TABLE IF NOT EXISTS comment_revision (
            comment_revision_id TEXT PRIMARY KEY,
            comment_id TEXT NOT NULL REFERENCES comment(comment_id),
            body TEXT NOT NULL,
            tags_json TEXT NOT NULL DEFAULT '[]',
            star_count INTEGER,
            star_checked_at TEXT,
            posted_at TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            content_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS asset (
            asset_id TEXT PRIMARY KEY,
            payload_id TEXT NOT NULL REFERENCES payload(payload_id),
            resource_id TEXT REFERENCES resource(resource_id),
            resource_revision_id TEXT REFERENCES resource_revision(resource_revision_id),
            alt_text TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE(payload_id, resource_id, source_url)
        );
        CREATE TABLE IF NOT EXISTS resource_image (
            resource_image_id TEXT PRIMARY KEY,
            resource_id TEXT NOT NULL REFERENCES resource(resource_id),
            resource_revision_id TEXT REFERENCES resource_revision(resource_revision_id),
            source_url TEXT NOT NULL,
            alt_text TEXT NOT NULL DEFAULT '',
            position INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            UNIQUE(resource_id, source_url)
        );
        CREATE TABLE IF NOT EXISTS resource_comment_state (
            provider TEXT NOT NULL,
            resource_id TEXT NOT NULL REFERENCES resource(resource_id),
            bookmark_count INTEGER NOT NULL,
            entry_url TEXT NOT NULL DEFAULT '',
            entry_id TEXT NOT NULL DEFAULT '',
            checked_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(provider, resource_id)
        );
        CREATE TABLE IF NOT EXISTS resource_relation (
            relation_id TEXT PRIMARY KEY,
            from_resource_id TEXT NOT NULL REFERENCES resource(resource_id),
            to_resource_id TEXT NOT NULL REFERENCES resource(resource_id),
            relation_type TEXT NOT NULL,
            confidence REAL,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            decided_by TEXT NOT NULL DEFAULT 'system',
            created_at TEXT NOT NULL,
            revoked_at TEXT
        );
        CREATE TABLE IF NOT EXISTS sync_run (
            sync_run_id TEXT PRIMARY KEY,
            providers_json TEXT NOT NULL,
            settings_fingerprint TEXT NOT NULL,
            status TEXT NOT NULL,
            error TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT
        );
        CREATE TABLE IF NOT EXISTS sync_run_item (
            sync_run_id TEXT NOT NULL REFERENCES sync_run(sync_run_id),
            source_item_id TEXT NOT NULL REFERENCES source_item(source_item_id),
            status TEXT NOT NULL,
            error TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(sync_run_id, source_item_id)
        );
        CREATE TABLE IF NOT EXISTS llm_run (
            llm_run_id TEXT PRIMARY KEY,
            resource_id TEXT REFERENCES resource(resource_id),
            resource_revision_id TEXT REFERENCES resource_revision(resource_revision_id),
            operation TEXT NOT NULL,
            backend TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            summary_schema_version TEXT NOT NULL,
            fingerprint_version INTEGER NOT NULL,
            input_fingerprint TEXT NOT NULL,
            request_json TEXT NOT NULL,
            response_json TEXT,
            result_json TEXT,
            usage_json TEXT,
            price_json TEXT,
            status TEXT NOT NULL,
            error TEXT,
            auth_mode TEXT NOT NULL,
            billing_mode TEXT NOT NULL,
            backend_metadata_json TEXT NOT NULL DEFAULT '{}',
            duration_ms INTEGER,
            retry_of_llm_run_id TEXT REFERENCES llm_run(llm_run_id),
            started_at TEXT NOT NULL,
            finished_at TEXT
        );
        CREATE TABLE IF NOT EXISTS source_note (
            source_note_id TEXT PRIMARY KEY,
            resource_id TEXT NOT NULL REFERENCES resource(resource_id),
            llm_run_id TEXT REFERENCES llm_run(llm_run_id),
            markdown TEXT NOT NULL,
            markdown_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            superseded_at TEXT
        );
        CREATE TABLE IF NOT EXISTS review_decision (
            review_decision_id TEXT PRIMARY KEY,
            resource_id TEXT NOT NULL REFERENCES resource(resource_id),
            decision TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            decided_by TEXT NOT NULL DEFAULT 'human',
            created_at TEXT NOT NULL,
            superseded_at TEXT
        );
        CREATE TABLE IF NOT EXISTS snapshot (
            snapshot_id TEXT PRIMARY KEY,
            manifest_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            verified_at TEXT
        );
        CREATE INDEX IF NOT EXISTS source_item_resource_idx ON source_item(resource_id);
        CREATE INDEX IF NOT EXISTS source_revision_item_idx ON source_item_revision(source_item_id, created_at);
        CREATE INDEX IF NOT EXISTS resource_revision_resource_idx ON resource_revision(resource_id, created_at);
        CREATE INDEX IF NOT EXISTS fetch_capture_resource_idx ON fetch_capture(resource_id, fetched_at);
        CREATE INDEX IF NOT EXISTS comment_resource_idx ON comment(resource_id);
        CREATE INDEX IF NOT EXISTS comment_revision_comment_idx ON comment_revision(comment_id);
        CREATE INDEX IF NOT EXISTS resource_image_resource_idx ON resource_image(resource_id, position);
        CREATE INDEX IF NOT EXISTS sync_run_status_idx ON sync_run(status, started_at);
        CREATE INDEX IF NOT EXISTS llm_run_reuse_idx ON llm_run(
            resource_revision_id, operation, backend, model, prompt_version,
            summary_schema_version, fingerprint_version, input_fingerprint, status
        );
        """
    )


def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
    """Convert the canonical database to latest-state storage.

    Revision-shaped tables remain, but only the row referenced by each parent is
    retained. Search data and downloaded image bytes are deliberately removed.
    """
    with _transaction(connection):
        if not _column_exists(connection, "comment_revision", "star_checked_at"):
            connection.execute("ALTER TABLE comment_revision ADD COLUMN star_checked_at TEXT")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS resource_image (
                resource_image_id TEXT PRIMARY KEY,
                resource_id TEXT NOT NULL REFERENCES resource(resource_id),
                resource_revision_id TEXT REFERENCES resource_revision(resource_revision_id),
                source_url TEXT NOT NULL,
                alt_text TEXT NOT NULL DEFAULT '',
                position INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                UNIQUE(resource_id, source_url)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS resource_comment_state (
                provider TEXT NOT NULL,
                resource_id TEXT NOT NULL REFERENCES resource(resource_id),
                bookmark_count INTEGER NOT NULL,
                checked_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(provider, resource_id)
            )
            """
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO resource_image(
                resource_image_id, resource_id, resource_revision_id, source_url, alt_text, position, updated_at
            )
            SELECT a.asset_id, a.resource_id, a.resource_revision_id, a.source_url, a.alt_text,
                   ROW_NUMBER() OVER (PARTITION BY a.resource_id ORDER BY a.created_at, a.asset_id) - 1,
                   a.created_at
            FROM asset AS a
            JOIN resource AS r ON r.resource_id = a.resource_id
            WHERE a.source_url <> '' AND a.resource_revision_id = r.current_revision_id
            """
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO resource_comment_state(provider, resource_id, bookmark_count, checked_at, updated_at)
            SELECT c.provider, c.resource_id,
                   MAX(CAST(json_extract(cr.metadata_json, '$.bookmark_count') AS INTEGER)),
                   MAX(c.updated_at), MAX(c.updated_at)
            FROM comment AS c
            JOIN comment_revision AS cr ON cr.comment_revision_id = c.current_revision_id
            WHERE json_type(cr.metadata_json, '$.bookmark_count') IN ('integer', 'real')
            GROUP BY c.provider, c.resource_id
            """
        )

        for table in ("resource_fts", "source_fts", "comment_fts"):
            connection.execute(f"DROP TABLE IF EXISTS {table}")

        connection.execute("DELETE FROM source_note WHERE superseded_at IS NOT NULL")
        connection.execute(
            "DELETE FROM source_item_revision WHERE source_revision_id NOT IN "
            "(SELECT current_revision_id FROM source_item WHERE current_revision_id IS NOT NULL)"
        )
        connection.execute(
            "DELETE FROM comment_revision WHERE comment_revision_id NOT IN "
            "(SELECT current_revision_id FROM comment WHERE current_revision_id IS NOT NULL)"
        )
        connection.execute(
            """
            UPDATE comment_revision
            SET star_checked_at = created_at
            WHERE star_count IS NOT NULL AND star_checked_at IS NULL
            """
        )
        connection.execute(
            """
            DELETE FROM fetch_capture
            WHERE fetch_capture_id NOT IN (
                SELECT fetch_capture_id FROM (
                    SELECT fetch_capture_id,
                           ROW_NUMBER() OVER (
                               PARTITION BY resource_id
                               ORDER BY (http_payload_id IS NOT NULL OR rendered_payload_id IS NOT NULL) DESC,
                                        fetched_at DESC, fetch_capture_id DESC
                           ) AS row_number
                    FROM fetch_capture
                ) WHERE row_number = 1
            )
            """
        )
        connection.execute(
            """
            UPDATE fetch_capture
            SET resource_revision_id = (
                SELECT current_revision_id FROM resource WHERE resource.resource_id = fetch_capture.resource_id
            )
            """
        )
        connection.execute("DELETE FROM asset")
        connection.execute(
            """
            UPDATE llm_run SET resource_revision_id = NULL
            WHERE resource_revision_id IS NOT NULL
              AND resource_revision_id NOT IN (
                  SELECT current_revision_id FROM resource WHERE current_revision_id IS NOT NULL
              )
            """
        )
        connection.execute(
            "DELETE FROM resource_revision WHERE resource_revision_id NOT IN "
            "(SELECT current_revision_id FROM resource WHERE current_revision_id IS NOT NULL)"
        )
        connection.execute(
            """
            DELETE FROM payload
            WHERE payload_id NOT IN (
                SELECT payload_id FROM source_item_revision WHERE payload_id IS NOT NULL
                UNION
                SELECT http_payload_id FROM fetch_capture WHERE http_payload_id IS NOT NULL
                UNION
                SELECT rendered_payload_id FROM fetch_capture WHERE rendered_payload_id IS NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS resource_image_resource_idx ON resource_image(resource_id, position)"
        )
        connection.execute(
            """
            INSERT INTO schema_meta(key, value) VALUES ('search_generation', '1')
            ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)
            """
        )
        connection.execute(
            "UPDATE schema_meta SET value = '2' WHERE key = 'schema_version'"
        )


def _column_exists(connection: sqlite3.Connection, table: str, column: str) -> bool:
    return any(str(row[1]) == column for row in connection.execute(f"PRAGMA table_info({table})"))


def _migrate_v2_to_v3(connection: sqlite3.Connection) -> None:
    """Discard reproducible HTML and the one-time legacy Markdown safety copy."""
    with _transaction(connection):
        connection.execute(
            """
            UPDATE fetch_capture
            SET http_payload_id = NULL
            WHERE http_payload_id IN (
                SELECT payload_id FROM payload WHERE lower(media_type) LIKE '%html%'
            )
            """
        )
        connection.execute("UPDATE fetch_capture SET rendered_payload_id = NULL")
        connection.execute(
            """
            DELETE FROM payload
            WHERE payload_id NOT IN (
                SELECT payload_id FROM source_item_revision WHERE payload_id IS NOT NULL
                UNION
                SELECT http_payload_id FROM fetch_capture WHERE http_payload_id IS NOT NULL
                UNION
                SELECT rendered_payload_id FROM fetch_capture WHERE rendered_payload_id IS NOT NULL
                UNION
                SELECT payload_id FROM asset WHERE payload_id IS NOT NULL
            )
            """
        )
        connection.execute("DROP TABLE IF EXISTS legacy_artifact")
        connection.execute("UPDATE schema_meta SET value = '3' WHERE key = 'schema_version'")


def _migrate_v3_to_v4(connection: sqlite3.Connection) -> None:
    """Normalize Hatena comment metadata and retain the top twenty comments per resource."""
    with _transaction(connection):
        if not _column_exists(connection, "comment_revision", "posted_at"):
            connection.execute("ALTER TABLE comment_revision ADD COLUMN posted_at TEXT NOT NULL DEFAULT ''")
        if not _column_exists(connection, "resource_comment_state", "entry_url"):
            connection.execute("ALTER TABLE resource_comment_state ADD COLUMN entry_url TEXT NOT NULL DEFAULT ''")
        if not _column_exists(connection, "resource_comment_state", "entry_id"):
            connection.execute("ALTER TABLE resource_comment_state ADD COLUMN entry_id TEXT NOT NULL DEFAULT ''")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS comment_revision_comment_idx ON comment_revision(comment_id)"
        )
        connection.execute(
            """
            UPDATE resource_comment_state
            SET entry_url = COALESCE((
                    SELECT json_extract(cr.metadata_json, '$.entry_url')
                    FROM comment AS c
                    JOIN comment_revision AS cr ON cr.comment_revision_id = c.current_revision_id
                    WHERE c.provider = resource_comment_state.provider
                      AND c.resource_id = resource_comment_state.resource_id
                      AND json_extract(cr.metadata_json, '$.entry_url') IS NOT NULL
                    LIMIT 1
                ), ''),
                entry_id = COALESCE((
                    SELECT json_extract(cr.metadata_json, '$.entry_id')
                    FROM comment AS c
                    JOIN comment_revision AS cr ON cr.comment_revision_id = c.current_revision_id
                    WHERE c.provider = resource_comment_state.provider
                      AND c.resource_id = resource_comment_state.resource_id
                      AND json_extract(cr.metadata_json, '$.entry_id') IS NOT NULL
                    LIMIT 1
                ), '')
            WHERE provider = 'hatena'
            """
        )
        connection.execute(
            """
            UPDATE comment_revision
            SET posted_at = COALESCE(json_extract(metadata_json, '$.timestamp'), ''),
                metadata_json = json_remove(
                    metadata_json,
                    '$.timestamp', '$.entry_url', '$.entry_id', '$.star_url', '$.bookmark_count'
                )
            WHERE comment_id IN (SELECT comment_id FROM comment WHERE provider = 'hatena')
            """
        )
        connection.execute("DROP TABLE IF EXISTS temp.comment_prune")
        connection.execute("CREATE TEMP TABLE comment_prune(comment_id TEXT PRIMARY KEY)")
        connection.execute(
            """
            INSERT INTO comment_prune(comment_id)
            SELECT comment_id
            FROM (
                SELECT c.comment_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY c.provider, c.resource_id
                           ORDER BY COALESCE(cr.star_count, 0) DESC,
                                    CASE WHEN cr.posted_at = '' THEN 1 ELSE 0 END,
                                    cr.posted_at ASC,
                                    c.comment_id ASC
                       ) AS rank
                FROM comment AS c
                JOIN comment_revision AS cr ON cr.comment_revision_id = c.current_revision_id
                WHERE c.provider = 'hatena' AND c.removed_at IS NULL
            )
            WHERE rank > 20
            """
        )
        connection.execute(
            "DELETE FROM comment_revision WHERE comment_id IN (SELECT comment_id FROM comment_prune)"
        )
        connection.execute("DELETE FROM comment WHERE comment_id IN (SELECT comment_id FROM comment_prune)")
        connection.execute("DROP TABLE comment_prune")

        rows = connection.execute(
            """
            SELECT cr.comment_revision_id, cr.body, cr.tags_json, cr.star_count,
                   cr.posted_at, cr.metadata_json
            FROM comment AS c
            JOIN comment_revision AS cr ON cr.comment_revision_id = c.current_revision_id
            WHERE c.provider = 'hatena'
            """
        ).fetchall()
        connection.executemany(
            "UPDATE comment_revision SET content_hash = ? WHERE comment_revision_id = ?",
            [
                (
                    _comment_content_hash(
                        body=str(row["body"]),
                        tags_json=str(row["tags_json"]),
                        star_count=int(row["star_count"]) if row["star_count"] is not None else None,
                        posted_at=str(row["posted_at"]),
                        metadata_json=str(row["metadata_json"]),
                    ),
                    str(row["comment_revision_id"]),
                )
                for row in rows
            ],
        )
        connection.execute(
            """
            INSERT INTO schema_meta(key, value) VALUES ('search_generation', '1')
            ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)
            """
        )
        connection.execute("UPDATE schema_meta SET value = '4' WHERE key = 'schema_version'")


def _migrate_v4_to_v5(connection: sqlite3.Connection) -> None:
    """Keep page validators so refreshes can use conditional HTTP requests."""
    with _transaction(connection):
        if not _column_exists(connection, "fetch_capture", "response_etag"):
            connection.execute("ALTER TABLE fetch_capture ADD COLUMN response_etag TEXT NOT NULL DEFAULT ''")
        if not _column_exists(connection, "fetch_capture", "response_last_modified"):
            connection.execute(
                "ALTER TABLE fetch_capture ADD COLUMN response_last_modified TEXT NOT NULL DEFAULT ''"
            )
        connection.execute("UPDATE schema_meta SET value = '5' WHERE key = 'schema_version'")


def _migrate_v5_to_v6(connection: sqlite3.Connection) -> None:
    """Add explicit backend identity, schema/fingerprint versions, and billing audit data."""

    with _transaction(connection):
        additions = (
            ("backend", "TEXT NOT NULL DEFAULT 'unknown'"),
            ("summary_schema_version", "TEXT NOT NULL DEFAULT 'unknown'"),
            ("fingerprint_version", "INTEGER NOT NULL DEFAULT 1"),
            ("auth_mode", "TEXT NOT NULL DEFAULT 'unknown'"),
            ("billing_mode", "TEXT NOT NULL DEFAULT 'unknown'"),
            ("backend_metadata_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("duration_ms", "INTEGER"),
        )
        for column, definition in additions:
            if not _column_exists(connection, "llm_run", column):
                connection.execute(f"ALTER TABLE llm_run ADD COLUMN {column} {definition}")
        connection.execute(
            """
            UPDATE llm_run
            SET backend = CASE
                    WHEN json_valid(request_json) AND json_extract(request_json, '$.provider') = 'openai'
                        THEN 'openai-responses'
                    WHEN json_valid(request_json) AND json_extract(request_json, '$.provider') = 'manus'
                        THEN 'manus-api'
                    WHEN json_valid(request_json) AND json_type(request_json, '$.agent_profile') IS NOT NULL
                        THEN 'manus-api'
                    WHEN json_valid(request_json) AND json_type(request_json, '$.input') IS NOT NULL
                         AND json_type(request_json, '$.text') IS NOT NULL
                        THEN 'openai-responses'
                    ELSE 'unknown'
                END,
                summary_schema_version = CASE
                    WHEN json_valid(request_json) AND (
                        json_extract(request_json, '$.provider') IN ('openai', 'manus')
                        OR json_type(request_json, '$.agent_profile') IS NOT NULL
                        OR (json_type(request_json, '$.input') IS NOT NULL
                            AND json_type(request_json, '$.text') IS NOT NULL)
                    ) THEN '1'
                    ELSE 'unknown'
                END,
                auth_mode = CASE
                    WHEN json_valid(request_json) AND (
                        json_extract(request_json, '$.provider') IN ('openai', 'manus')
                        OR json_type(request_json, '$.agent_profile') IS NOT NULL
                        OR (json_type(request_json, '$.input') IS NOT NULL
                            AND json_type(request_json, '$.text') IS NOT NULL)
                    ) THEN 'api-key'
                    ELSE 'unknown'
                END,
                billing_mode = CASE
                    WHEN json_valid(request_json) AND (
                        json_extract(request_json, '$.provider') IN ('openai', 'manus')
                        OR json_type(request_json, '$.agent_profile') IS NOT NULL
                        OR (json_type(request_json, '$.input') IS NOT NULL
                            AND json_type(request_json, '$.text') IS NOT NULL)
                    ) THEN 'metered-api'
                    ELSE 'unknown'
                END
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS llm_run_reuse_idx ON llm_run(
                resource_revision_id, operation, backend, model, prompt_version,
                summary_schema_version, fingerprint_version, input_fingerprint, status
            )
            """
        )
        connection.execute("UPDATE schema_meta SET value = '6' WHERE key = 'schema_version'")
