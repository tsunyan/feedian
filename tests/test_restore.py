from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from feedian.restore import _sha256, download_and_restore, restore_database
from feedian.snapshots import _find_7zip
from feedian.snapshots import _run as _snapshots_run
from feedian.vault import initialize_vault, vault_paths


def _build_valid_archive(tmp_path: Path, *, name: str = "snapshot") -> tuple[Path, str]:
    """Build a real .sqlite3.7z archive with a valid SQLite DB and matching manifest.

    Uses the real 7-Zip binary (as `create_snapshot` does) so the downstream
    "7z test -> extract -> manifest sha256 -> integrity_check" path in
    restore_database runs against a genuine archive. Returns
    (archive_path, archive_sha256).
    """
    work = tmp_path / f"{name}-build"
    work.mkdir()
    database = work / "feedian.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    manifest_path = work / "manifest.json"
    manifest_path.write_text(json.dumps({"database": {"sha256": _sha256(database)}}), encoding="utf-8")
    archive_path = work / f"{name}.sqlite3.7z"
    seven_zip = _find_7zip()
    _snapshots_run([str(seven_zip), "a", "-t7z", "-bd", "-y", str(archive_path), str(database), str(manifest_path)])
    return archive_path, _sha256(archive_path)


def _fake_show(tag: str, archive_sha256: str):
    def fake_run_git(_vault_root, args):
        assert args == ["show", f"{tag}:.feedian/snapshot.json"]
        payload = json.dumps({"archive": {"sha256": archive_sha256}})
        return subprocess.CompletedProcess(args, 0, stdout=payload, stderr="")

    return fake_run_git


def test_restore_refuses_to_overwrite_live_database(tmp_path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    initialize_vault(root)
    database = root / ".feedian" / "feedian.sqlite3"
    database.write_bytes(b"live")

    with pytest.raises(FileExistsError, match="live database"):
        restore_database(root, tmp_path / "missing.sqlite3.7z", "irrelevant-tag")


def test_restore_fails_before_extraction_on_archive_sha256_mismatch(tmp_path, monkeypatch) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    initialize_vault(root)
    archive_path, _archive_sha256 = _build_valid_archive(tmp_path)

    monkeypatch.setattr("feedian.restore._run_git", _fake_show("test-tag", "0" * 64))

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("7-Zip must not run before the Git-anchored checksum is verified")

    monkeypatch.setattr("feedian.restore._find_7zip", fail_if_called)

    with pytest.raises(RuntimeError, match="checksum does not match"):
        restore_database(root, archive_path, "test-tag")

    assert not (root / ".feedian" / "tmp" / "restore").exists()


def test_restore_proceeds_through_db_checks_when_archive_sha256_matches(tmp_path, monkeypatch) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    initialize_vault(root)
    archive_path, archive_sha256 = _build_valid_archive(tmp_path)

    monkeypatch.setattr("feedian.restore._run_git", _fake_show("test-tag", archive_sha256))

    database_path = restore_database(root, archive_path, "test-tag")

    assert database_path == vault_paths(root).database_path
    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute("SELECT name FROM sqlite_master WHERE name = 't'").fetchone() is not None
        assert str(connection.execute("PRAGMA integrity_check").fetchone()[0]) == "ok"
    finally:
        connection.close()


def test_restore_fails_when_tag_does_not_exist_locally(tmp_path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    initialize_vault(root)
    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True, capture_output=True, text=True)
    archive_path, _archive_sha256 = _build_valid_archive(tmp_path)

    with pytest.raises(RuntimeError, match=r"Could not read \.feedian/snapshot\.json"):
        restore_database(root, archive_path, "does-not-exist")


@pytest.mark.parametrize("payload", ["null", "[]", '"oops"', '{"archive": "abc"}', '{"archive": []}'])
def test_restore_raises_valueerror_not_attributeerror_on_malformed_manifest(tmp_path, monkeypatch, payload) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    initialize_vault(root)
    archive_path, _archive_sha256 = _build_valid_archive(tmp_path)

    def fake_run_git(_vault_root, args):
        return subprocess.CompletedProcess(args, 0, stdout=payload, stderr="")

    monkeypatch.setattr("feedian.restore._run_git", fake_run_git)

    with pytest.raises(ValueError, match="missing archive.sha256"):
        restore_database(root, archive_path, "test-tag")


def test_restore_rejects_an_empty_tag_instead_of_reading_the_local_index(tmp_path) -> None:
    """`git show ":path"` (empty ref before the colon) reads the local index
    instead of failing, which would silently make an untrusted working-tree
    file the trust anchor. An empty --tag must be rejected before it reaches git."""
    root = tmp_path / "vault"
    root.mkdir()
    initialize_vault(root)
    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True, capture_output=True, text=True)
    archive_path, _archive_sha256 = _build_valid_archive(tmp_path)

    for empty in ("", "   "):
        with pytest.raises(ValueError, match="non-empty tag is required"):
            restore_database(root, archive_path, empty)


def test_download_and_restore_fetches_the_tag_from_origin_before_restoring(tmp_path, monkeypatch) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    initialize_vault(root)
    archive_path, archive_sha256 = _build_valid_archive(tmp_path, name="release")

    calls: list[list[str]] = []
    show = _fake_show("release-tag", archive_sha256)

    def fake_run_git(vault_root, args):
        calls.append(args)
        if args[:1] == ["fetch"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return show(vault_root, args)

    monkeypatch.setattr("feedian.restore._run_git", fake_run_git)
    monkeypatch.setattr("feedian.restore._github_repository", lambda _root: "owner/repo")

    def fake_run(command, *, cwd=None):
        if command[0] == "gh":
            assert command[:3] == ["gh", "release", "download"]
            target_dir = Path(command[command.index("--dir") + 1])
            shutil.copy(archive_path, target_dir / archive_path.name)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return _snapshots_run(command, cwd=cwd)

    monkeypatch.setattr("feedian.restore._run", fake_run)

    database_path = download_and_restore(root, "release-tag")

    assert database_path == vault_paths(root).database_path
    assert ["fetch", "origin", "tag", "release-tag"] in calls
