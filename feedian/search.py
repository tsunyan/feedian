from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .store import VaultStore, utc_now


SEARCH_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class SearchIndexReport:
    rebuilt: bool
    generation: int
    sources: int
    resources: int
    comments: int
    path: Path


def rebuild_search_index(
    store: VaultStore, path: str | Path, *, force: bool = False
) -> SearchIndexReport:
    target = Path(path)
    generation = store.search_generation()
    if not force:
        current = search_index_generation(target)
        if current == generation:
            counts = _search_counts(target)
            return SearchIndexReport(False, generation, *counts, target)

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    temporary.unlink(missing_ok=True)
    connection = sqlite3.connect(temporary)
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.executescript(
            """
            CREATE TABLE search_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE VIRTUAL TABLE resource_fts USING fts5(
                resource_id UNINDEXED, title, content, tokenize='trigram'
            );
            CREATE VIRTUAL TABLE source_fts USING fts5(
                source_item_id UNINDEXED, title, comment, tags, tokenize='trigram'
            );
            CREATE VIRTUAL TABLE comment_fts USING fts5(
                comment_id UNINDEXED, body, tags, tokenize='trigram'
            );
            """
        )
        source_rows = store.connection.execute(
            """
            SELECT s.source_item_id, sr.metadata_json
            FROM source_item AS s
            JOIN source_item_revision AS sr ON sr.source_revision_id = s.current_revision_id
            WHERE s.removed_at IS NULL
            """
        )
        sources = 0
        for row in source_rows:
            metadata = json.loads(str(row["metadata_json"]))
            connection.execute(
                "INSERT INTO source_fts(source_item_id, title, comment, tags) VALUES (?, ?, ?, ?)",
                (
                    row["source_item_id"],
                    str(metadata.get("title") or ""),
                    str(metadata.get("note") or ""),
                    " ".join(str(tag) for tag in metadata.get("tags") or []),
                ),
            )
            sources += 1

        resource_rows = store.connection.execute(
            """
            SELECT r.resource_id, rr.title, rr.content_markdown
            FROM resource AS r
            JOIN resource_revision AS rr ON rr.resource_revision_id = r.current_revision_id
            WHERE r.removed_at IS NULL
            """
        )
        resources = 0
        for row in resource_rows:
            connection.execute(
                "INSERT INTO resource_fts(resource_id, title, content) VALUES (?, ?, ?)",
                (row["resource_id"], row["title"], row["content_markdown"]),
            )
            resources += 1

        comment_rows = store.connection.execute(
            """
            SELECT c.comment_id, cr.body, cr.tags_json
            FROM comment AS c
            JOIN comment_revision AS cr ON cr.comment_revision_id = c.current_revision_id
            WHERE c.removed_at IS NULL
            """
        )
        comments = 0
        for row in comment_rows:
            connection.execute(
                "INSERT INTO comment_fts(comment_id, body, tags) VALUES (?, ?, ?)",
                (
                    row["comment_id"],
                    row["body"],
                    " ".join(str(tag) for tag in json.loads(str(row["tags_json"]))),
                ),
            )
            comments += 1

        connection.executemany(
            "INSERT INTO search_meta(key, value) VALUES (?, ?)",
            [
                ("schema_version", str(SEARCH_SCHEMA_VERSION)),
                ("vault_id", store.vault_id()),
                ("source_generation", str(generation)),
                ("built_at", utc_now()),
            ],
        )
        connection.commit()
        if str(connection.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
            raise RuntimeError("Search index integrity check failed.")
    finally:
        connection.close()
    os.replace(temporary, target)
    return SearchIndexReport(True, generation, sources, resources, comments, target)


def search_index_generation(path: str | Path) -> int | None:
    target = Path(path)
    if not target.exists():
        return None
    try:
        connection = sqlite3.connect(f"file:{target.as_posix()}?mode=ro", uri=True)
        row = connection.execute(
            "SELECT value FROM search_meta WHERE key = 'source_generation'"
        ).fetchone()
        return int(row[0]) if row is not None else None
    except (sqlite3.Error, ValueError):
        return None
    finally:
        if "connection" in locals():
            connection.close()


def _search_counts(path: Path) -> tuple[int, int, int]:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        return tuple(
            int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("source_fts", "resource_fts", "comment_fts")
        )
    finally:
        connection.close()
