from __future__ import annotations

import pytest

from feedian.cli import main
from feedian.store import VaultStore
from feedian.__main__ import main as package_main


def test_no_arguments_show_help_without_creating_files(capsys, tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    assert main([]) == 0

    assert "usage: feedian" in capsys.readouterr().out
    assert not (tmp_path / ".feedian").exists()


def test_package_help_uses_modern_command_help(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        package_main(["--help"])
    assert exc_info.value.code == 0

    output = capsys.readouterr().out
    assert "snapshot" in output
    assert "Collect external sources into a per-vault SQLite archive" in output


def test_init_migrate_and_status(tmp_path, capsys) -> None:
    root = tmp_path / "vault"
    root.mkdir()

    assert main(["init", "--vault", str(root)]) == 0
    assert main(["migrate", "--vault", str(root)]) == 0
    assert main(["status", "--vault", str(root)]) == 0

    output = capsys.readouterr().out
    assert "initialized:" in output
    assert "migrated: schema_version=1" in output
    assert "integrity: ok" in output


def test_migrate_preserves_existing_raw_markdown(tmp_path, capsys) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    assert main(["init", "--vault", str(root)]) == 0
    legacy = root / "raw" / "Raindrop" / "before.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"original\r\n")

    assert main(["migrate", "--vault", str(root)]) == 0

    assert "legacy_imported=1" in capsys.readouterr().out
    store = VaultStore.open(root / ".feedian" / "feedian.sqlite3")
    try:
        assert store.connection.execute("SELECT content FROM legacy_artifact").fetchone()[0] == b"original\r\n"
    finally:
        store.close()


def test_set_default_vault_requires_initialized_vault(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    root = tmp_path / "vault"
    root.mkdir()
    assert main(["init", "--vault", str(root)]) == 0

    assert main(["config", "set-default-vault", str(root)]) == 0
