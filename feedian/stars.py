from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable

from .hatena import fetch_hatena_star_counts
from .store import VaultStore


@dataclass(frozen=True)
class StarEnrichmentReport:
    processed: int = 0
    updated: int = 0
    unavailable: int = 0


def enrich_hatena_stars(
    store: VaultStore, *, limit: int | None = None, progress: Callable[[int], None] | None = None
) -> StarEnrichmentReport:
    rows = store.connection.execute(
        """
        SELECT c.provider, c.resource_id, c.author, cr.body, cr.tags_json, cr.metadata_json
        FROM comment AS c
        JOIN comment_revision AS cr ON cr.comment_revision_id = c.current_revision_id
        WHERE c.provider = 'hatena' AND c.removed_at IS NULL AND cr.star_count IS NULL
        ORDER BY c.created_at, c.comment_id
        """
    ).fetchall()
    if limit is not None:
        rows = rows[:limit]
    updated = unavailable = 0
    processed = 0
    for offset in range(0, len(rows), 500):
        batch = rows[offset : offset + 500]
        star_urls = [str(json.loads(str(row["metadata_json"])).get("star_url") or "") for row in batch]
        counts = fetch_hatena_star_counts(star_urls)
        updates: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
        for row, star_url in zip(batch, star_urls):
            metadata = json.loads(str(row["metadata_json"]))
            if not star_url or star_url not in counts:
                unavailable += 1
            else:
                key = (str(row["provider"]), str(row["resource_id"]))
                updates[key].append(
                    {
                        "author": str(row["author"]),
                        "body": str(row["body"]),
                        "tags": [str(tag) for tag in json.loads(str(row["tags_json"]))],
                        "star_count": counts[star_url],
                        "metadata": metadata,
                    }
                )
                updated += 1
            processed += 1
            if progress is not None:
                progress(processed)
        for (provider, resource_id), comments in updates.items():
            # Star enrichment changes ranking metadata, not searchable text.
            store.upsert_comments(
                provider=provider,
                resource_id=resource_id,
                comments=comments,
                refresh_fts=False,
            )
    return StarEnrichmentReport(len(rows), updated, unavailable)
