from __future__ import annotations

import pytest

from feedian.cli import main
from feedian.store import VaultStore
from feedian.__main__ import main as package_main


def test_no_arguments_show_help_without_creating_files(capsys, tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    assert main([]) == 0

    output = capsys.readouterr().out
    assert "FEEDIAN" in output
    assert "feedian COMMAND [OPTIONS]" in output
    assert "sources" in output and "SQLite" in output and "Obsidian" in output
    assert not (tmp_path / ".feedian").exists()


def test_package_help_uses_modern_command_help(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        package_main(["--help"])
    assert exc_info.value.code == 0

    output = capsys.readouterr().out
    assert "snapshot" in output
    assert "Collect external sources into a per-vault SQLite archive" in output


def test_command_help_uses_the_same_rich_layout(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["sync", "--help"])
    assert exc_info.value.code == 0

    output = capsys.readouterr().out
    assert "FEEDIAN  /  sync" in output
    assert "Usage" in output
    assert "Options" in output
    assert "--source" in output


def test_write_lock_error_is_human_readable(tmp_path, capsys) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    assert main(["init", "--vault", str(root)]) == 0
    assert main(["migrate", "--vault", str(root)]) == 0

    from feedian.locking import vault_write_lock

    with vault_write_lock(root / ".feedian"):
        assert main(["render", "--vault", str(root), "--apply", "--progress", "off"]) == 1

    error = capsys.readouterr().err
    assert "Vault is busy" in error
    assert "Process" in error
    assert "Started" in error
    assert "Lock" in error
    assert '{"pid"' not in error


def test_init_migrate_and_status(tmp_path, capsys) -> None:
    root = tmp_path / "vault"
    root.mkdir()

    assert main(["init", "--vault", str(root)]) == 0
    assert main(["migrate", "--vault", str(root)]) == 0
    assert main(["status", "--vault", str(root)]) == 0

    output = capsys.readouterr().out
    assert "initialized:" in output
    assert "migrated: schema_version=4" in output
    assert "integrity: ok" in output


def test_migrate_does_not_copy_existing_raw_markdown_into_sqlite(tmp_path, capsys) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    assert main(["init", "--vault", str(root)]) == 0
    legacy = root / "raw" / "Raindrop" / "before.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"original\r\n")

    assert main(["migrate", "--vault", str(root)]) == 0

    assert "migrated: schema_version=4" in capsys.readouterr().out
    store = VaultStore.open(root / ".feedian" / "feedian.sqlite3")
    try:
        assert not store.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'legacy_artifact'"
        ).fetchone()
        assert legacy.read_bytes() == b"original\r\n"
    finally:
        store.close()


def test_set_default_vault_requires_initialized_vault(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    root = tmp_path / "vault"
    root.mkdir()
    assert main(["init", "--vault", str(root)]) == 0

    assert main(["config", "set-default-vault", str(root)]) == 0
