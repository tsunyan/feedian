from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SOURCE_STATE_PATH = Path.home() / ".feedian" / "source-state.json"


def load_raindrop_collection_count(
    token: str,
    collection_id: int,
    nested: bool,
    *,
    state_path: Path = SOURCE_STATE_PATH,
) -> int | None:
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    counts = data.get("raindrop_collection_counts") if isinstance(data, dict) else None
    entry = counts.get(_raindrop_key(token, collection_id, nested)) if isinstance(counts, dict) else None
    count = entry.get("count") if isinstance(entry, dict) else None
    return count if isinstance(count, int) and count >= 0 else None


def save_raindrop_collection_count(
    token: str,
    collection_id: int,
    nested: bool,
    count: int,
    *,
    state_path: Path = SOURCE_STATE_PATH,
) -> None:
    data: dict[str, Any] = {}
    try:
        loaded = json.loads(state_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        pass

    counts = data.get("raindrop_collection_counts")
    if not isinstance(counts, dict):
        counts = {}
        data["raindrop_collection_counts"] = counts
    data["version"] = 1
    counts[_raindrop_key(token, collection_id, nested)] = {
        "count": max(0, int(count)),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_path.with_suffix(state_path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(state_path)


def _raindrop_key(token: str, collection_id: int, nested: bool) -> str:
    account = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
    return f"{account}:collection={collection_id}:nested={str(bool(nested)).lower()}"
