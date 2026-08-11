from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


@contextmanager
def vault_write_lock(state_dir: str | Path) -> Iterator[None]:
    """Acquire the single writer lock for a Vault without deleting another run's lock."""
    path = Path(state_dir) / "feedian.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError as exc:
        try:
            holder = path.read_text(encoding="utf-8").strip()
        except OSError:
            holder = "unknown holder"
        raise RuntimeError(f"Another Feedian write operation is already running ({holder or 'unknown holder'}).") from exc
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
