from __future__ import annotations

import json

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
    assert "migrated: schema_version=9" in output
    assert "integrity: ok" in output


def test_status_reports_unreachable_using_the_vaults_terminal_http_statuses(tmp_path, capsys) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    assert main(["init", "--vault", str(root)]) == 0
    assert main(["migrate", "--vault", str(root)]) == 0

    store = VaultStore.open(root / ".feedian" / "feedian.sqlite3")
    try:
        # Terminal by the vault's default terminal_http_statuses (404, 410):
        # counted.
        gone = store.upsert_canonical_item(
            CanonicalItem(
                source="hatena", source_id="gone", content_key="url:gone",
                url="https://example.test/gone", title="Gone",
            )
        )
        store.record_failed_fetch(gone.resource_id or "", warning="HTTP 404", http_status=404)
        # Not a terminal status: not counted.
        alive = store.upsert_canonical_item(
            CanonicalItem(
                source="hatena", source_id="alive", content_key="url:alive",
                url="https://example.test/alive", title="Alive",
            )
        )
        store.record_failed_fetch(alive.resource_id or "", warning="HTTP 500", http_status=500)
    finally:
        store.close()

    assert main(["status", "--vault", str(root)]) == 0

    output = capsys.readouterr().out
    assert "unreachable: 1" in output


def test_status_reports_unreachable_using_the_vaults_terminal_failure_kinds(tmp_path, capsys) -> None:
    """The count has to follow this vault's kinds and threshold, not the defaults.

    Every seeded resource is deliberately classified differently by the vault's
    settings than by terminal_failure_count's own defaults, so a status command
    that forgets to pass the configured values reports 1 instead of 2 rather
    than landing on the same answer by luck.
    """

    root = tmp_path / "vault"
    root.mkdir()
    assert main(["init", "--vault", str(root)]) == 0
    assert main(["migrate", "--vault", str(root)]) == 0

    # No terminal HTTP statuses at all, so whatever this counts comes from the
    # failure-kind half of the rule -- the case that reports 0 if the two
    # halves are treated as anything but a union.
    config_path = root / ".feedian" / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["fetch"]["terminal_http_statuses"] = []
    config["fetch"]["terminal_failure_kinds"] = ["dns"]
    config["fetch"]["terminal_kind_failures"] = 2
    config_path.write_text(json.dumps(config), encoding="utf-8")

    store = VaultStore.open(root / ".feedian" / "feedian.sqlite3")

    def seed(name: str, kind: str, times: int) -> None:
        stored = store.upsert_canonical_item(
            CanonicalItem(
                source="hatena", source_id=name, content_key=f"url:{name}",
                url=f"https://example.test/{name}", title=name,
            )
        )
        for _ in range(times):
            store.record_failed_fetch(stored.resource_id or "", warning=f"failed: {kind}", failure_kind=kind)

    try:
        # Terminal here (dns, 2 >= 2); below the default threshold of 3.
        seed("dead-one", "dns", 2)
        seed("dead-two", "dns", 2)
        # Right kind, one failure short of this vault's threshold.
        seed("early", "dns", 1)
        # Past every threshold, but "timeout" is not among this vault's kinds --
        # counted only if the defaults leak through.
        seed("slow", "timeout", 3)
    finally:
        store.close()

    assert main(["status", "--vault", str(root)]) == 0

    assert "unreachable: 2" in capsys.readouterr().out


def test_status_rejects_an_invalid_terminal_http_statuses_config(tmp_path, capsys) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    assert main(["init", "--vault", str(root)]) == 0

    config_path = root / ".feedian" / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["fetch"]["terminal_http_statuses"] = [99]
    config_path.write_text(json.dumps(config), encoding="utf-8")

    assert main(["status", "--vault", str(root)]) == 1

    error = capsys.readouterr().err
    assert "fetch.terminal_http_statuses" in error


def test_migrate_does_not_copy_existing_raw_markdown_into_sqlite(tmp_path, capsys) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    assert main(["init", "--vault", str(root)]) == 0
    legacy = root / "raw" / "Raindrop" / "before.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"original\r\n")

    assert main(["migrate", "--vault", str(root)]) == 0

    assert "migrated: schema_version=9" in capsys.readouterr().out
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


def test_restore_without_tag_is_rejected_by_argument_parsing(tmp_path) -> None:
    root = tmp_path / "vault"
    root.mkdir()

    with pytest.raises(SystemExit) as exc_info:
        main(["restore", "--vault", str(root), "--archive", "foo.7z"])
    assert exc_info.value.code == 2


def test_restore_with_archive_and_tag_restores_from_the_local_archive(tmp_path, monkeypatch) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    assert main(["init", "--vault", str(root)]) == 0

    captured: list[tuple] = []

    def fake_restore_database(vault_root, archive, tag):
        captured.append((vault_root, archive, tag))
        return root / ".feedian" / "feedian.sqlite3"

    def fail_download_and_restore(*_args, **_kwargs):
        raise AssertionError("download_and_restore must not run when --archive is given")

    monkeypatch.setattr("feedian.cli.restore_database", fake_restore_database)
    monkeypatch.setattr("feedian.cli.download_and_restore", fail_download_and_restore)

    assert main(["restore", "--vault", str(root), "--archive", "foo.7z", "--tag", "some-tag"]) == 0
    assert captured == [(root.resolve(), "foo.7z", "some-tag")]


def test_restore_with_tag_only_downloads_from_the_release(tmp_path, monkeypatch) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    assert main(["init", "--vault", str(root)]) == 0

    captured: list[tuple] = []

    def fake_download_and_restore(vault_root, tag):
        captured.append((vault_root, tag))
        return root / ".feedian" / "feedian.sqlite3"

    def fail_restore_database(*_args, **_kwargs):
        raise AssertionError("restore_database must not run directly when only --tag is given")

    monkeypatch.setattr("feedian.cli.download_and_restore", fake_download_and_restore)
    monkeypatch.setattr("feedian.cli.restore_database", fail_restore_database)

    assert main(["restore", "--vault", str(root), "--tag", "some-tag"]) == 0
    assert captured == [(root.resolve(), "some-tag")]


def test_set_default_vault_requires_initialized_vault(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    root = tmp_path / "vault"
    root.mkdir()
    assert main(["init", "--vault", str(root)]) == 0

    assert main(["config", "set-default-vault", str(root)]) == 0


@pytest.mark.parametrize(
    "argv",
    [
        ["sync", "--force-fetch"],
        ["sync", "--quick", "--force-fetch"],
        ["sync", "--quick", "--force-comments"],
        ["sync", "--full", "--quick"],
    ],
)
def test_sync_flag_combinations_that_conflict_with_quick_mode_exit_with_code_two(argv) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(argv)
    assert exc_info.value.code == 2


def test_full_with_force_fetch_passes_argument_validation(tmp_path, monkeypatch) -> None:
    from feedian.sync import SyncReport

    root = tmp_path / "vault"
    root.mkdir()
    assert main(["init", "--vault", str(root)]) == 0
    assert main(["migrate", "--vault", str(root)]) == 0

    captured: list[dict] = []

    def fake_sync_vault(store, config, **kwargs):
        captured.append(kwargs)
        return SyncReport(run_id="run", processed=0, changed=0, failed=0, fetched=0)

    monkeypatch.setattr("feedian.cli.sync_vault", fake_sync_vault)

    assert main(["sync", "--vault", str(root), "--full", "--force-fetch", "--progress", "off"]) == 0
    assert captured[0]["force_fetch"] is True


def test_run_pipeline_still_syncs_full_after_the_cli_default_flipped_to_quick(tmp_path, monkeypatch) -> None:
    import argparse

    from feedian.cli import _run_pipeline
    from feedian.renderer import RenderReport
    from feedian.stars import StarEnrichmentReport
    from feedian.sync import SyncReport

    root = tmp_path / "vault"
    root.mkdir()
    assert main(["init", "--vault", str(root)]) == 0
    assert main(["migrate", "--vault", str(root)]) == 0

    captured: list[dict] = []

    def fake_sync_vault(store, config, **kwargs):
        captured.append(kwargs)
        return SyncReport(run_id="run", processed=0, changed=0, failed=0, fetched=0)

    monkeypatch.setattr("feedian.cli.sync_vault", fake_sync_vault)
    monkeypatch.setattr("feedian.cli._due_providers", lambda _store, _config: ["hatena"])
    monkeypatch.setattr("feedian.cli._snapshot_is_due", lambda _store: False)
    monkeypatch.setattr("feedian.cli.enrich_hatena_stars", lambda *_args, **_kwargs: StarEnrichmentReport())
    monkeypatch.setattr("feedian.cli.rebuild_search_index", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "feedian.cli.render_raw_views",
        lambda *_args, **_kwargs: RenderReport(written=0, skipped=0, conflicts=0, comments_written=0, output_root=root),
    )
    monkeypatch.setattr("feedian.cli.notify_windows", lambda *_args, **_kwargs: None)

    args = argparse.Namespace(vault=str(root), if_due=False, skip_snapshot=True)

    assert _run_pipeline(args) == 0
    assert captured
    assert not captured[0].get("quick", False)
