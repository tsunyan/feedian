from __future__ import annotations

import pytest

from feedian.canonical import CanonicalItem
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


def test_package_help_uses_modern_command_help(capsys, monkeypatch) -> None:
    # argparse wraps its help to COLUMNS, splitting the phrases asserted below.
    monkeypatch.setenv("COLUMNS", "300")
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


def test_unpriced_ingest_reads_as_unknown_not_free() -> None:
    from feedian.cli_ui import ingest_cost_text, ingest_cost_value, ingest_tokens_text
    from feedian.ingest import IngestReport

    unpriced = IngestReport(processed=2, created=2, unpriced_requests=2, unmetered_requests=2)
    priced = IngestReport(processed=1, created=1, input_tokens=120, output_tokens=34, cost_usd=0.00123)
    mixed = IngestReport(processed=2, created=2, input_tokens=120, cost_usd=0.00123, unpriced_requests=1)

    assert ingest_cost_text(unpriced) == "n/a"
    assert ingest_cost_value(unpriced) == "n/a"
    assert ingest_tokens_text(unpriced) == "in n/a | out n/a"
    assert ingest_cost_text(priced) == "$0.001230"
    assert ingest_tokens_text(priced) == "in 120 | out 34"
    assert "unpriced" in ingest_cost_text(mixed)
    assert ingest_cost_value(mixed) == "0.001230"


def test_init_migrate_and_status(tmp_path, capsys) -> None:
    root = tmp_path / "vault"
    root.mkdir()

    assert main(["init", "--vault", str(root)]) == 0
    assert main(["migrate", "--vault", str(root)]) == 0
    assert main(["status", "--vault", str(root)]) == 0

    output = capsys.readouterr().out
    assert "initialized:" in output
    assert "migrated: schema_version=6" in output
    assert "integrity: ok" in output


def test_migrate_does_not_copy_existing_raw_markdown_into_sqlite(tmp_path, capsys) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    assert main(["init", "--vault", str(root)]) == 0
    legacy = root / "raw" / "Raindrop" / "before.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"original\r\n")

    assert main(["migrate", "--vault", str(root)]) == 0

    assert "migrated: schema_version=6" in capsys.readouterr().out
    store = VaultStore.open(root / ".feedian" / "feedian.sqlite3")
    try:
        assert not store.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'legacy_artifact'"
        ).fetchone()
        assert legacy.read_bytes() == b"original\r\n"
    finally:
        store.close()


def test_ingest_dry_run_prints_plan_without_writes(tmp_path, capsys, monkeypatch) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.chdir(tmp_path)
    # The plan is a four-column Rich grid; a narrow terminal wraps the model name
    # out of the row this test reads back.
    monkeypatch.setenv("COLUMNS", "300")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.6-sol")
    assert main(["init", "--vault", str(root)]) == 0
    assert main(["migrate", "--vault", str(root)]) == 0
    store = VaultStore.open(root / ".feedian" / "feedian.sqlite3")
    try:
        item = store.upsert_canonical_item(
            CanonicalItem(
                source="hatena", source_id="one", content_key="url:one",
                url="https://example.test", title="Dry Run Article", tags=["python"],
            )
        )
        store.record_resource_revision(item.resource_id or "", content_markdown="Body", title="Dry Run Article")
    finally:
        store.close()

    assert main(["ingest", "--vault", str(root), "--dry-run", "--auto", "--limit", "1"]) == 0

    output = capsys.readouterr().out
    assert "INGEST" in output and "PREVIEW" in output
    assert "Command" in output
    assert "feedian ingest --vault" in output
    assert "--dry-run --auto --limit 1" in output
    assert "Auto select" in output
    assert "API calls" in output and "1" in output
    assert "Maximum" in output and "$" in output
    assert "python" in output and "New field" in output
    assert "Dry Run Article" in output
    assert "gpt-5.6-sol" in output
    store = VaultStore.open(root / ".feedian" / "feedian.sqlite3")
    try:
        assert store.connection.execute("SELECT COUNT(*) FROM llm_run").fetchone()[0] == 0
        assert store.connection.execute("SELECT COUNT(*) FROM source_note").fetchone()[0] == 0
    finally:
        store.close()


def test_set_default_vault_requires_initialized_vault(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    root = tmp_path / "vault"
    root.mkdir()
    assert main(["init", "--vault", str(root)]) == 0

    assert main(["config", "set-default-vault", str(root)]) == 0
