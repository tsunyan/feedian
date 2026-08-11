from __future__ import annotations

import pytest

from feedian.restore import restore_database
from feedian.vault import initialize_vault


def test_restore_refuses_to_overwrite_live_database(tmp_path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    initialize_vault(root)
    database = root / ".feedian" / "feedian.sqlite3"
    database.write_bytes(b"live")

    with pytest.raises(FileExistsError, match="live database"):
        restore_database(root, tmp_path / "missing.sqlite3.7z")
