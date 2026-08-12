from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .ids import uuid7
from .store import VaultStore, stable_json
from .vault import VaultConfig, vault_paths


GITHUB_REMOTE_PATTERN = re.compile(
    r"^(?:git@github\.com:|https://github\.com/)(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SnapshotReport:
    snapshot_id: str
    tag: str
    archive_path: Path | None
    archive_sha256: str | None
    dry_run: bool = False


SnapshotProgress = Callable[[str, int, int, bool], None]


def create_snapshot(
    store: VaultStore,
    vault_root: str | Path,
    config: VaultConfig,
    *,
    dry_run: bool = False,
    progress: SnapshotProgress | None = None,
) -> SnapshotReport:
    """Archive a consistent DB backup and verify it after private Release upload.

    This never stages arbitrary vault files: only configured generated folders
    and the two explicitly versioned .feedian JSON files are included.
    """
    total_phases = 1 if dry_run else 9
    paths = vault_paths(vault_root)
    _phase(progress, "checking prerequisites", 1, total_phases)
    repository = _github_repository(paths.root)
    _assert_private_repository(repository)
    seven_zip = _find_7zip()
    _require_clean_staging_area(paths.root, config)
    if store.integrity_check() != "ok":
        raise RuntimeError("Live database integrity check failed; refusing to snapshot.")
    _phase(progress, "checking prerequisites", 1, total_phases, completed=True)

    created_at = datetime.now(timezone.utc).replace(microsecond=0)
    snapshot_id = uuid7()
    tag = f"feedian-snapshot-{created_at:%Y%m%dT%H%M%SZ}-{snapshot_id[:8]}"
    if dry_run:
        return SnapshotReport(snapshot_id, tag, None, None, dry_run=True)

    _phase(progress, "preparing snapshot metadata", 2, total_phases)
    previous = store.latest_snapshot()
    manifest = _base_manifest(store, snapshot_id, tag, created_at, previous)
    store.record_snapshot(snapshot_id, manifest)
    work_dir = paths.state_dir / "tmp" / snapshot_id
    work_dir.mkdir(parents=True, exist_ok=False)
    database_backup = work_dir / "feedian.sqlite3"
    archive_manifest = work_dir / "manifest.json"
    archive_path = work_dir / f"{tag}.sqlite3.7z"
    _phase(progress, "preparing snapshot metadata", 2, total_phases, completed=True)
    try:
        _phase(progress, "backing up SQLite", 3, total_phases)
        store.backup_to(database_backup)
        _phase(progress, "backing up SQLite", 3, total_phases, completed=True)

        _phase(progress, "compressing database archive", 4, total_phases)
        database_sha256 = _sha256_file(database_backup)
        manifest["database"] = {"sha256": database_sha256, "byte_length": database_backup.stat().st_size}
        archive_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        _run([str(seven_zip), "a", "-t7z", "-bd", "-y", str(archive_path), str(database_backup), str(archive_manifest)])
        archive_sha256 = _sha256_file(archive_path)
        manifest["archive"] = {"filename": archive_path.name, "sha256": archive_sha256, "byte_length": archive_path.stat().st_size}
        paths.state_dir.mkdir(parents=True, exist_ok=True)
        (paths.state_dir / "snapshot.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        _phase(progress, "compressing database archive", 4, total_phases, completed=True)

        _phase(progress, "committing Vault snapshot", 5, total_phases)
        _stage_snapshot_paths(paths.root, config)
        _commit_if_needed(paths.root, f"snapshot: {created_at:%Y-%m-%d %H:%M UTC}")
        _run_git(paths.root, ["tag", "-a", tag, "-m", f"Feedian snapshot {snapshot_id}"])
        _phase(progress, "committing Vault snapshot", 5, total_phases, completed=True)

        _phase(progress, "pushing commit and tag", 6, total_phases)
        _run_git(paths.root, ["push", "origin", "HEAD"])
        _run_git(paths.root, ["push", "origin", tag])
        _phase(progress, "pushing commit and tag", 6, total_phases, completed=True)

        _phase(progress, "publishing private GitHub Release", 7, total_phases)
        _run(
            [
                "gh", "release", "create", tag, str(archive_path), "--repo", repository,
                "--title", f"Feedian snapshot {created_at:%Y-%m-%d %H:%M UTC}",
                "--notes", f"SQLite archive for snapshot `{snapshot_id}`.", "--verify-tag",
            ]
        )
        _phase(progress, "publishing private GitHub Release", 7, total_phases, completed=True)

        _phase(progress, "downloading and verifying Release", 8, total_phases)
        _verify_remote_archive(repository, tag, archive_path, archive_sha256, seven_zip, work_dir)
        _phase(progress, "downloading and verifying Release", 8, total_phases, completed=True)

        _phase(progress, "finalizing verified snapshot", 9, total_phases)
        store.mark_snapshot_verified(snapshot_id)
        shutil.rmtree(work_dir)
        _phase(progress, "finalizing verified snapshot", 9, total_phases, completed=True)
    except Exception as exc:
        raise RuntimeError(f"Snapshot failed; temporary archive retained at {work_dir}: {exc}") from exc
    return SnapshotReport(snapshot_id, tag, None, archive_sha256)


def _phase(
    progress: SnapshotProgress | None,
    description: str,
    phase: int,
    total_phases: int,
    *,
    completed: bool = False,
) -> None:
    if progress is not None:
        progress(description, phase, total_phases, completed)


def _base_manifest(
    store: VaultStore,
    snapshot_id: str,
    tag: str,
    created_at: datetime,
    previous: sqlite3.Row | None,
) -> dict[str, Any]:
    return {
        "snapshot_id": snapshot_id,
        "created_at": created_at.isoformat().replace("+00:00", "Z"),
        "vault_id": store.vault_id(),
        "schema_version": store.schema_version(),
        "feedian": _feedian_build_info(),
        "tag": tag,
        "database": {},
        "archive": {},
        "record_counts": store.status_counts(),
        "latest_sync_run": _row_to_json(store.latest_sync_run()),
        "previous_snapshot_id": previous["snapshot_id"] if previous is not None else None,
    }


def _feedian_build_info() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    try:
        version = importlib.metadata.version("feedian")
    except importlib.metadata.PackageNotFoundError:
        version = "development"
    commit = _git_output(root, ["rev-parse", "HEAD"], required=False)
    dirty = bool(_git_output(root, ["status", "--porcelain"], required=False))
    return {"version": version, "commit": commit or None, "dirty": dirty}


def _github_repository(vault_root: Path) -> str:
    remote = _git_output(vault_root, ["remote", "get-url", "origin"])
    match = GITHUB_REMOTE_PATTERN.match(remote.strip())
    if match is None:
        raise RuntimeError("Vault origin must be a GitHub SSH or HTTPS remote.")
    return f"{match.group('owner')}/{match.group('repo')}"


def _assert_private_repository(repository: str) -> None:
    result = _run(["gh", "repo", "view", repository, "--json", "isPrivate", "--jq", ".isPrivate"])
    if result.stdout.strip().lower() != "true":
        raise RuntimeError(f"Refusing snapshot: GitHub repository {repository} is not private.")


def _find_7zip() -> Path:
    configured = Path(str(__import__("os").environ.get("FEEDIAN_7Z", ""))).expanduser()
    candidates = [configured] if str(configured) not in {"", "."} else []
    candidates.extend(
        candidate for candidate in (
            shutil.which("7z"),
            r"C:\Program Files\7-Zip\7z.exe",
            r"C:\Program Files (x86)\7-Zip\7z.exe",
        ) if candidate
    )
    for candidate in candidates:
        path = Path(candidate)
        if path.is_file():
            return path
    raise FileNotFoundError("7-Zip was not found. Install 7-Zip or set FEEDIAN_7Z.")


def _require_clean_staging_area(vault_root: Path, config: VaultConfig) -> None:
    staged = _git_output(vault_root, ["diff", "--cached", "--name-only"])
    allowed = _managed_git_paths(config)
    unexpected = [path for path in staged.splitlines() if path and not any(_path_is_within(path, item) for item in allowed)]
    if unexpected:
        raise RuntimeError("Refusing snapshot with unrelated staged files: " + ", ".join(unexpected))


def _stage_snapshot_paths(vault_root: Path, config: VaultConfig) -> None:
    existing = [path for path in _managed_git_paths(config) if (vault_root / path).exists()]
    if existing:
        _run_git(vault_root, ["add", "--", *existing])


def _managed_git_paths(config: VaultConfig) -> list[str]:
    return [config.raw_folder, config.source_folder, ".feedian/config.json", ".feedian/snapshot.json"]


def _path_is_within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _commit_if_needed(vault_root: Path, message: str) -> None:
    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"], cwd=vault_root, text=True, capture_output=True, check=False
    )
    if result.returncode == 0:
        return
    if result.returncode != 1:
        raise RuntimeError(result.stderr.strip() or "Could not inspect staged Git changes.")
    _run_git(vault_root, ["commit", "-m", message])


def _verify_remote_archive(
    repository: str, tag: str, expected_path: Path, expected_sha256: str, seven_zip: Path, work_dir: Path
) -> None:
    download_dir = work_dir / "verified-download"
    download_dir.mkdir()
    _run(["gh", "release", "download", tag, "--repo", repository, "--pattern", expected_path.name, "--dir", str(download_dir)])
    downloaded = download_dir / expected_path.name
    if not downloaded.is_file() or _sha256_file(downloaded) != expected_sha256:
        raise RuntimeError("Downloaded Release asset does not match the local archive checksum.")
    _run([str(seven_zip), "t", str(downloaded)])
    extracted = work_dir / "verified-extracted"
    extracted.mkdir()
    _run([str(seven_zip), "e", "-y", f"-o{extracted}", str(downloaded), "feedian.sqlite3"])
    database = extracted / "feedian.sqlite3"
    connection = sqlite3.connect(database)
    try:
        if str(connection.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
            raise RuntimeError("Downloaded SQLite archive failed integrity check.")
    finally:
        connection.close()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _run_git(vault_root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return _run(["git", *args], cwd=vault_root)


def _git_output(vault_root: Path, args: list[str], *, required: bool = True) -> str:
    try:
        return _run_git(vault_root, args).stdout.strip()
    except RuntimeError:
        if required:
            raise
        return ""


def _run(command: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=True)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Required command not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "command failed").strip()
        raise RuntimeError(f"{' '.join(command[:3])}: {detail}") from exc


def _row_to_json(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None
