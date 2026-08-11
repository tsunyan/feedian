from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

from .snapshots import _find_7zip, _github_repository, _run
from .vault import vault_paths


def restore_database(vault_root: str | Path, archive: str | Path) -> Path:
    """Restore one verified database archive into a Vault with no live database."""
    paths = vault_paths(vault_root)
    if paths.database_path.exists():
        raise FileExistsError(f"Refusing restore because a live database already exists: {paths.database_path}")
    source = Path(archive).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Snapshot archive not found: {source}")
    seven_zip = _find_7zip()
    temporary = paths.state_dir / "tmp" / "restore"
    if temporary.exists():
        raise FileExistsError(f"Restore staging directory already exists: {temporary}")
    temporary.mkdir(parents=True)
    try:
        _run([str(seven_zip), "t", str(source)])
        _run([str(seven_zip), "e", "-y", f"-o{temporary}", str(source), "feedian.sqlite3", "manifest.json"])
        restored = temporary / "feedian.sqlite3"
        manifest_path = temporary / "manifest.json"
        if not restored.is_file() or not manifest_path.is_file():
            raise RuntimeError("Archive is missing feedian.sqlite3 or manifest.json.")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected = ((manifest.get("database") or {}).get("sha256"))
        if not isinstance(expected, str) or _sha256(restored) != expected:
            raise RuntimeError("Restored database checksum does not match the archive manifest.")
        connection = sqlite3.connect(restored)
        try:
            if str(connection.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
                raise RuntimeError("Restored database failed SQLite integrity check.")
        finally:
            connection.close()
        paths.database_path.parent.mkdir(parents=True, exist_ok=True)
        restored.replace(paths.database_path)
        return paths.database_path
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def download_and_restore(vault_root: str | Path, tag: str) -> Path:
    root = Path(vault_root).resolve()
    paths = vault_paths(root)
    repository = _github_repository(root)
    temporary = paths.state_dir / "tmp" / "release-download"
    if temporary.exists():
        raise FileExistsError(f"Restore download directory already exists: {temporary}")
    temporary.mkdir(parents=True)
    try:
        _run(["gh", "release", "download", tag, "--repo", repository, "--pattern", "*.sqlite3.7z", "--dir", str(temporary)])
        archives = list(temporary.glob("*.sqlite3.7z"))
        if len(archives) != 1:
            raise RuntimeError(f"Expected exactly one SQLite archive in Release {tag}, found {len(archives)}.")
        return restore_database(root, archives[0])
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
