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


SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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
    def open(cls, path: str | Path) -> "VaultStore":
        database_path = Path(path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        store = cls(connection, database_path)
        store.migrate()
        return store

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            yield self.connection
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def migrate(self) -> None:
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
        elif current < SCHEMA_VERSION:
            raise RuntimeError("Database migration is required; run `feedian migrate --vault ...`.")

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
        digest = sha256_bytes(content)
        row = self.connection.execute("SELECT payload_id FROM payload WHERE sha256 = ?", (digest,)).fetchone()
        if row is not None:
            return str(row["payload_id"])
        payload_id = uuid7()
        with self.transaction() as connection:
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
        payload_id = (
            self.put_payload(source_payload, media_type=source_media_type)
            if source_payload is not None
            else None
        )
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
                    "SELECT metadata_hash FROM source_item_revision WHERE source_revision_id = ?",
                    (source_row["current_revision_id"],),
                ).fetchone()
                previous_hash = str(previous_hash_row["metadata_hash"]) if previous_hash_row else None
                connection.execute(
                    "UPDATE source_item SET resource_id = ?, updated_at = ?, removed_at = NULL WHERE source_item_id = ?",
                    (resource_id, now, source_item_id),
                )
            changed = metadata_hash != previous_hash
            if changed:
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
        self._refresh_source_fts(source_item_id, item)
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
    ) -> tuple[str, bool]:
        content_hash = sha256_bytes(
            stable_json({"content_markdown": content_markdown, "discussion_text": discussion_text}).encode("utf-8")
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
            if current_hash_row is not None and current_hash_row["content_hash"] == content_hash:
                revision_id = str(current_id)
                connection.execute(
                    """
                    INSERT INTO fetch_capture(fetch_capture_id, resource_id, resource_revision_id, http_payload_id, rendered_payload_id,
                                              final_url, extracted_by, content_truncated, warning, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (uuid7(), resource_id, revision_id, http_payload_id, rendered_payload_id, final_url, extracted_by,
                     int(content_truncated), warning, now),
                )
                return revision_id, False
            revision_id = uuid7()
            connection.execute(
                """
                INSERT INTO resource_revision(resource_revision_id, resource_id, title, content_markdown, discussion_text, content_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (revision_id, resource_id, title, content_markdown, discussion_text, content_hash, now),
            )
            connection.execute(
                "UPDATE resource SET current_revision_id = ?, updated_at = ? WHERE resource_id = ?",
                (revision_id, now, resource_id),
            )
            connection.execute(
                """
                INSERT INTO fetch_capture(fetch_capture_id, resource_id, resource_revision_id, http_payload_id, rendered_payload_id,
                                          final_url, extracted_by, content_truncated, warning, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (uuid7(), resource_id, revision_id, http_payload_id, rendered_payload_id, final_url, extracted_by,
                 int(content_truncated), warning, now),
            )
        self._refresh_resource_fts(resource_id, title, content_markdown)
        return revision_id, True

    def upsert_comment(
        self,
        *,
        provider: str,
        resource_id: str,
        author: str,
        body: str,
        tags: list[str] | None = None,
        star_count: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[str, bool]:
        if not author:
            raise ValueError("Comment author must not be empty.")
        tags_json = stable_json(tags or [])
        metadata_json = stable_json(metadata or {})
        content_hash = sha256_bytes(
            stable_json({"body": body, "tags": tags_json, "star_count": star_count, "metadata": metadata_json}).encode("utf-8")
        )
        now = utc_now()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT comment_id, current_revision_id FROM comment WHERE provider = ? AND resource_id = ? AND author = ?",
                (provider, resource_id, author),
            ).fetchone()
            if row is None:
                comment_id = uuid7()
                connection.execute(
                    """
                    INSERT INTO comment(comment_id, provider, resource_id, author, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (comment_id, provider, resource_id, author, now, now),
                )
                current_hash = None
            else:
                comment_id = str(row["comment_id"])
                current_hash_row = connection.execute(
                    "SELECT content_hash FROM comment_revision WHERE comment_revision_id = ?",
                    (row["current_revision_id"],),
                ).fetchone()
                current_hash = str(current_hash_row["content_hash"]) if current_hash_row else None
                connection.execute(
                    "UPDATE comment SET updated_at = ?, removed_at = NULL WHERE comment_id = ?", (now, comment_id)
                )
            changed = current_hash != content_hash
            if changed:
                revision_id = uuid7()
                connection.execute(
                    """
                    INSERT INTO comment_revision(comment_revision_id, comment_id, body, tags_json, star_count, metadata_json, content_hash, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (revision_id, comment_id, body, tags_json, star_count, metadata_json, content_hash, now),
                )
                connection.execute("UPDATE comment SET current_revision_id = ? WHERE comment_id = ?", (revision_id, comment_id))
        self._refresh_comment_fts(comment_id, body, " ".join(tags or []))
        return comment_id, changed

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
            "resource", "resource_revision", "source_item", "source_item_revision", "comment", "asset", "payload",
            "legacy_artifact", "llm_run", "source_note", "snapshot",
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
            SELECT fetched_at, warning, http_payload_id, rendered_payload_id
            FROM fetch_capture WHERE resource_id = ? ORDER BY fetched_at DESC LIMIT 1
            """,
            (resource_id,),
        ).fetchone()
        if latest is None:
            return True
        fetched_at = datetime.fromisoformat(str(latest["fetched_at"]).replace("Z", "+00:00"))
        if latest["warning"] and not latest["http_payload_id"] and not latest["rendered_payload_id"]:
            return datetime.now(timezone.utc) - fetched_at >= timedelta(minutes=30)
        return datetime.now(timezone.utc) - fetched_at >= timedelta(days=max(1, refresh_days))

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
        self, *, resource_revision_id: str, operation: str, model: str, prompt_version: str, input_fingerprint: str
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT result_json FROM llm_run
            WHERE resource_revision_id = ? AND operation = ? AND model = ? AND prompt_version = ?
              AND input_fingerprint = ? AND status = 'completed'
            ORDER BY finished_at DESC LIMIT 1
            """,
            (resource_revision_id, operation, model, prompt_version, input_fingerprint),
        ).fetchone()
        return json.loads(str(row["result_json"])) if row is not None and row["result_json"] else None

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
    ) -> str:
        run_id = uuid7()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO llm_run(llm_run_id, resource_id, resource_revision_id, operation, model, prompt_version,
                                    input_fingerprint, request_json, status, started_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', ?)
                """,
                (run_id, resource_id, resource_revision_id, operation, model, prompt_version, input_fingerprint,
                 stable_json(request), utc_now()),
            )
        return run_id

    def finish_llm_run(
        self,
        run_id: str,
        *,
        response: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        usage: dict[str, Any] | None = None,
        price: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        status = "failed" if error else "completed"
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE llm_run SET response_json = ?, result_json = ?, usage_json = ?, price_json = ?, status = ?, error = ?, finished_at = ?
                WHERE llm_run_id = ?
                """,
                (stable_json(response) if response is not None else None, stable_json(result) if result is not None else None,
                 stable_json(usage) if usage is not None else None, stable_json(price) if price is not None else None,
                 status, error, utc_now(), run_id),
            )

    def put_source_note(self, *, resource_id: str, llm_run_id: str | None, markdown: str) -> str:
        markdown_hash = sha256_bytes(markdown.encode("utf-8"))
        current = self.connection.execute(
            "SELECT source_note_id, markdown_hash FROM source_note WHERE resource_id = ? AND superseded_at IS NULL",
            (resource_id,),
        ).fetchone()
        if current is not None and current["markdown_hash"] == markdown_hash:
            return str(current["source_note_id"])
        note_id = uuid7()
        now = utc_now()
        with self.transaction() as connection:
            if current is not None:
                connection.execute("UPDATE source_note SET superseded_at = ? WHERE source_note_id = ?", (now, current["source_note_id"]))
            connection.execute(
                """
                INSERT INTO source_note(source_note_id, resource_id, llm_run_id, markdown, markdown_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (note_id, resource_id, llm_run_id, markdown, markdown_hash, now),
            )
        return note_id

    def put_asset(
        self,
        *,
        resource_id: str,
        resource_revision_id: str,
        content: bytes,
        media_type: str,
        source_url: str,
        alt_text: str = "",
        headers: dict[str, str] | None = None,
    ) -> str:
        payload_id = self.put_payload(
            content, media_type=media_type, source_url=source_url, headers=headers,
        )
        row = self.connection.execute(
            "SELECT asset_id FROM asset WHERE payload_id = ? AND resource_id = ? AND source_url = ?",
            (payload_id, resource_id, source_url),
        ).fetchone()
        if row is not None:
            return str(row["asset_id"])
        asset_id = uuid7()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO asset(asset_id, payload_id, resource_id, resource_revision_id, alt_text, source_url, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (asset_id, payload_id, resource_id, resource_revision_id, alt_text, source_url, utc_now()),
            )
        return asset_id

    def import_legacy_artifacts(self, raw_root: str | Path) -> tuple[int, int]:
        """Preserve pre-Feedian raw Markdown exactly once before first rendering.

        Legacy files are deliberately not parsed or rewritten here. They are an
        immutable recovery record; normalisation happens only after a later
        provider re-collection has produced separately staged views.
        """
        root = Path(raw_root)
        if not root.exists():
            return 0, 0
        imported = skipped = 0
        for path in sorted(candidate for candidate in root.rglob("*.md") if candidate.is_file()):
            relative_path = path.relative_to(root).as_posix()
            row = self.connection.execute(
                "SELECT sha256 FROM legacy_artifact WHERE relative_path = ?", (relative_path,)
            ).fetchone()
            if row is not None:
                skipped += 1
                continue
            content = path.read_bytes()
            with self.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO legacy_artifact(legacy_artifact_id, relative_path, content, sha256, mtime_ns, imported_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (uuid7(), relative_path, content, sha256_bytes(content), path.stat().st_mtime_ns, utc_now()),
                )
            imported += 1
        return imported, skipped

    def matches_legacy_artifact(self, relative_path: str, content: bytes) -> bool:
        row = self.connection.execute(
            "SELECT sha256, content FROM legacy_artifact WHERE relative_path = ?", (relative_path.replace("\\", "/"),)
        ).fetchone()
        return bool(row is not None and row["content"] == content and row["sha256"] == sha256_bytes(content))

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

    def _refresh_resource_fts(self, resource_id: str, title: str, content: str) -> None:
        try:
            with self.transaction() as connection:
                connection.execute("DELETE FROM resource_fts WHERE resource_id = ?", (resource_id,))
                connection.execute(
                    "INSERT INTO resource_fts(resource_id, title, content) VALUES (?, ?, ?)",
                    (resource_id, title, content),
                )
        except sqlite3.OperationalError:
            pass

    def _refresh_source_fts(self, source_item_id: str, item: CanonicalItem) -> None:
        try:
            with self.transaction() as connection:
                connection.execute("DELETE FROM source_fts WHERE source_item_id = ?", (source_item_id,))
                connection.execute(
                    "INSERT INTO source_fts(source_item_id, title, comment, tags) VALUES (?, ?, ?, ?)",
                    (source_item_id, item.title, item.comment, " ".join(item.tags)),
                )
        except sqlite3.OperationalError:
            pass

    def _refresh_comment_fts(self, comment_id: str, body: str, tags: str) -> None:
        try:
            with self.transaction() as connection:
                connection.execute("DELETE FROM comment_fts WHERE comment_id = ?", (comment_id,))
                connection.execute(
                    "INSERT INTO comment_fts(comment_id, body, tags) VALUES (?, ?, ?)", (comment_id, body, tags)
                )
        except sqlite3.OperationalError:
            pass


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
            model TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            input_fingerprint TEXT NOT NULL,
            request_json TEXT NOT NULL,
            response_json TEXT,
            result_json TEXT,
            usage_json TEXT,
            price_json TEXT,
            status TEXT NOT NULL,
            error TEXT,
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
        CREATE TABLE IF NOT EXISTS legacy_artifact (
            legacy_artifact_id TEXT PRIMARY KEY,
            relative_path TEXT NOT NULL UNIQUE,
            content BLOB NOT NULL,
            sha256 TEXT NOT NULL,
            mtime_ns INTEGER,
            imported_at TEXT NOT NULL
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
        CREATE INDEX IF NOT EXISTS sync_run_status_idx ON sync_run(status, started_at);
        """
    )
    try:
        connection.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS resource_fts USING fts5(resource_id UNINDEXED, title, content, tokenize='trigram')"
        )
        connection.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS source_fts USING fts5(source_item_id UNINDEXED, title, comment, tags, tokenize='trigram')"
        )
        connection.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS comment_fts USING fts5(comment_id UNINDEXED, body, tags, tokenize='trigram')"
        )
    except sqlite3.OperationalError:
        # Python builds without FTS5 retain the canonical tables. Search can fall back to SQL LIKE.
        pass
