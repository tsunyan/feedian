from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from .hatena import fetch_hatena_star_counts, hatena_comment_star_url
from .store import VaultStore


@dataclass(frozen=True)
class StarEnrichmentReport:
    processed: int = 0
    updated: int = 0
    unavailable: int = 0


def enrich_hatena_stars(
    store: VaultStore,
    *,
    limit: int | None = None,
    refresh_days: int = 30,
    force: bool = False,
    progress: Callable[[int], None] | None = None,
) -> StarEnrichmentReport:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, refresh_days))).isoformat()
    rows = store.connection.execute(
        """
        SELECT c.comment_id, c.author, cr.posted_at, rcs.entry_id
        FROM comment AS c
        JOIN comment_revision AS cr ON cr.comment_revision_id = c.current_revision_id
        JOIN resource_comment_state AS rcs
          ON rcs.provider = c.provider AND rcs.resource_id = c.resource_id
        WHERE c.provider = 'hatena' AND c.removed_at IS NULL
          AND (? OR cr.star_checked_at IS NULL OR cr.star_checked_at < ?)
        ORDER BY c.created_at, c.comment_id
        """,
        (int(force), cutoff),
    ).fetchall()
    if limit is not None:
        rows = rows[:limit]
    updated = unavailable = 0
    processed = 0
    for offset in range(0, len(rows), 500):
        batch = rows[offset : offset + 500]
        star_urls = [
            hatena_comment_star_url(
                str(row["author"]), str(row["posted_at"]), str(row["entry_id"])
            )
            for row in batch
        ]
        counts = fetch_hatena_star_counts(star_urls)
        updates: dict[str, int | None] = {}
        for row, star_url in zip(batch, star_urls):
            if not star_url or star_url not in counts:
                unavailable += 1
                updates[str(row["comment_id"])] = None
            else:
                updates[str(row["comment_id"])] = counts[star_url]
                updated += 1
            processed += 1
            if progress is not None:
                progress(processed)
        store.update_comment_star_counts(updates)
    return StarEnrichmentReport(len(rows), updated, unavailable)
