from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PendingTransaction:
    transaction_id: str
    target: Path
    markdown: str
    usage_record: dict[str, Any]


def pending_path(destination: Path, state_root: Path | None = None) -> Path:
    root = state_root or (Path.home() / ".raindian" / "pending")
    canonical_destination = str(destination.resolve())
    digest = hashlib.sha256(canonical_destination.encode("utf-8")).hexdigest()
    return root / f"{digest}.json"


def save_pending(
    destination: Path,
    transaction: PendingTransaction,
    *,
    state_root: Path | None = None,
) -> None:
    path = pending_path(destination, state_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "transaction_id": transaction.transaction_id,
        "target": str(transaction.target.resolve()),
        "markdown": transaction.markdown,
        "usage_record": transaction.usage_record,
    }
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
            newline="\n",
        )
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def load_pending(
    destination: Path,
    *,
    state_root: Path | None = None,
) -> PendingTransaction | None:
    path = pending_path(destination, state_root)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid pending transaction: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"invalid pending transaction: {path}")

    transaction_id = payload.get("transaction_id")
    target_value = payload.get("target")
    markdown = payload.get("markdown")
    usage_record = payload.get("usage_record")
    if (
        not isinstance(transaction_id, str)
        or not transaction_id
        or not isinstance(target_value, str)
        or not isinstance(markdown, str)
        or not isinstance(usage_record, dict)
        or usage_record.get("transaction_id") != transaction_id
    ):
        raise ValueError(f"invalid pending transaction: {path}")

    target = Path(target_value).resolve()
    try:
        target.relative_to(destination.resolve())
    except ValueError as exc:
        raise ValueError(f"pending target is outside destination: {path}") from exc
    return PendingTransaction(transaction_id, target, markdown, usage_record)


def remove_pending(destination: Path, *, state_root: Path | None = None) -> None:
    pending_path(destination, state_root).unlink(missing_ok=True)
