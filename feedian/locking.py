from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


class VaultWriteLockError(RuntimeError):
    def __init__(self, lock_path: Path, holder: dict[str, object] | None = None) -> None:
        self.lock_path = lock_path
        self.holder = holder or {}
        raw_pid = self.holder.get("pid")
        self.pid = int(raw_pid) if isinstance(raw_pid, int) or str(raw_pid or "").isdigit() else None
        self.started_at = str(self.holder.get("started_at") or "")
        super().__init__("Another Feedian write operation is already running.")


@contextmanager
def vault_write_lock(state_dir: str | Path) -> Iterator[None]:
    """Acquire the single writer lock for a Vault without deleting another run's lock."""
    path = Path(state_dir) / "feedian.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError as exc:
        try:
            raw_holder = json.loads(path.read_text(encoding="utf-8"))
            holder = raw_holder if isinstance(raw_holder, dict) else {}
        except (OSError, json.JSONDecodeError):
            holder = {}
        raise VaultWriteLockError(path, holder) from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(
                json.dumps({"pid": os.getpid(), "started_at": datetime.now(timezone.utc).isoformat()}) + "\n"
            )
        yield
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
