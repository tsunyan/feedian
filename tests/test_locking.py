from __future__ import annotations

import pytest

from feedian.locking import vault_write_lock


def test_vault_write_lock_prevents_a_second_writer(tmp_path) -> None:
    with vault_write_lock(tmp_path):
        with pytest.raises(RuntimeError, match="already running"):
            with vault_write_lock(tmp_path):
                pass
    assert not (tmp_path / "feedian.lock").exists()
