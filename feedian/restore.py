from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

from .snapshots import _find_7zip, _github_repository, _run, _run_git
from .vault import vault_paths


def restore_database(vault_root: str | Path, archive: str | Path, tag: str) -> Path:
    """Restore one verified database archive into a Vault with no live database.

    Threat model: verification trusts the Git repository's commit/tag history
    as the integrity anchor outside the archive being restored -- Git tags and
    commits are assumed genuine, and Release archives are the only thing that
    can be corrupted or tampered with. This does not verify Git's own history
    or require signed tags: a tag name is a mutable reference, not a
    signature, and a compromised Git repository is out of scope. What this
    check does close is the archive-only attack, where a tampered database
    ships alongside a manifest.json rewritten to match it -- that manifest
    lives inside the same archive being restored and cannot detect tampering
    on its own, so the archive's own sha256 must be checked against a value
    recorded outside the archive (the tagged `.feedian/snapshot.json`) before
    anything is extracted.
    """
    paths = vault_paths(vault_root)
    if paths.database_path.exists():
        raise FileExistsError(f"Refusing restore because a live database already exists: {paths.database_path}")
    source = Path(archive).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Snapshot archive not found: {source}")
    trusted_sha256 = _trusted_archive_sha256(paths.root, tag)
    actual_sha256 = _sha256(source)
    if actual_sha256 != trusted_sha256:
        raise RuntimeError(
            f"Archive checksum does not match the Git-tagged snapshot manifest for tag {tag!r}: "
            f"expected {trusted_sha256}, got {actual_sha256}."
        )
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
    """Fetch a Release archive and restore it, ensuring the tag is locally available first.

    A fresh clone, or a tag created and pushed from a different machine, may
    not have the tag locally yet -- `restore_database` requires `git show
    <tag>:...` to succeed, so this fetches the tag from origin before doing
    anything else.
    """
    root = Path(vault_root).resolve()
    paths = vault_paths(root)
    repository = _github_repository(root)
    _run_git(root, ["fetch", "origin", "tag", tag])
    temporary = paths.state_dir / "tmp" / "release-download"
    if temporary.exists():
        raise FileExistsError(f"Restore download directory already exists: {temporary}")
    temporary.mkdir(parents=True)
    try:
        _run(["gh", "release", "download", tag, "--repo", repository, "--pattern", "*.sqlite3.7z", "--dir", str(temporary)])
        archives = list(temporary.glob("*.sqlite3.7z"))
        if len(archives) != 1:
            raise RuntimeError(f"Expected exactly one SQLite archive in Release {tag}, found {len(archives)}.")
        return restore_database(root, archives[0], tag)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def _trusted_archive_sha256(vault_root: Path, tag: str) -> str:
    """Read the archive's expected sha256 from the Git-tagged snapshot manifest.

    This is the anchor outside the archive: `.feedian/snapshot.json` as it was
    committed and tagged by `create_snapshot`, read via `git show`, not from
    any copy bundled inside the archive itself. Raises if the tag does not
    exist locally, the path is missing at that tag, or the content is not the
    expected JSON shape -- there is no fallback.
    """
    try:
        result = _run_git(vault_root, ["show", f"{tag}:.feedian/snapshot.json"])
    except RuntimeError as exc:
        raise RuntimeError(
            f"Could not read .feedian/snapshot.json at tag {tag!r}. "
            f"Ensure the tag exists locally (git fetch --tags) and the vault is Git-managed: {exc}"
        ) from exc
    try:
        manifest = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f".feedian/snapshot.json at tag {tag!r} is not valid JSON: {exc}") from exc
    sha256 = (manifest.get("archive") or {}).get("sha256")
    if not isinstance(sha256, str) or not sha256:
        raise ValueError(f".feedian/snapshot.json at tag {tag!r} is missing archive.sha256.")
    return sha256


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
