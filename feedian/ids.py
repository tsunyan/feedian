from __future__ import annotations

import secrets
import time
import uuid


def uuid7() -> str:
    """Return a sortable UUIDv7 string without requiring a newer Python runtime."""
    timestamp_ms = int(time.time() * 1000)
    if timestamp_ms >= 1 << 48:
        raise RuntimeError("system clock is outside the UUIDv7 timestamp range")
    random_a = secrets.randbits(12)
    random_b = secrets.randbits(62)
    value = (
        (timestamp_ms << 80)
        | (0x7 << 76)
        | (random_a << 64)
        | (0b10 << 62)
        | random_b
    )
    return str(uuid.UUID(int=value))
