from __future__ import annotations

import uuid

from feedian.ids import uuid7


def test_uuid7_returns_rfc_compatible_version_7_uuid() -> None:
    value = uuid.UUID(uuid7())

    assert value.version == 7
    assert value.variant == "specified in RFC 4122"
