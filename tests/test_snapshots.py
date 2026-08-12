from __future__ import annotations

from pathlib import Path

import pytest

from feedian.snapshots import _github_repository, _managed_git_paths, _path_is_within, _phase
from feedian.vault import VaultConfig


def test_managed_snapshot_paths_exclude_review_and_database() -> None:
    paths = _managed_git_paths(VaultConfig())

    assert paths == ["raw", "source", ".feedian/config.json", ".feedian/snapshot.json"]
    assert _path_is_within("raw/Hatena/note.md", "raw")
    assert not _path_is_within("review/note.md", "raw")
    assert not _path_is_within(".feedian/feedian.sqlite3", ".feedian/snapshot.json")


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("git@github.com:tsunyan/at.git", "tsunyan/at"),
        ("https://github.com/tsunyan/at.git", "tsunyan/at"),
    ],
)
def test_github_repository_uses_origin(monkeypatch, tmp_path: Path, remote: str, expected: str) -> None:
    monkeypatch.setattr("feedian.snapshots._git_output", lambda *_args, **_kwargs: remote)

    assert _github_repository(tmp_path) == expected


def test_snapshot_phase_reports_start_and_completion() -> None:
    events: list[tuple[str, int, int, bool]] = []

    _phase(lambda *event: events.append(event), "compressing database archive", 4, 9)
    _phase(lambda *event: events.append(event), "compressing database archive", 4, 9, completed=True)

    assert events == [
        ("compressing database archive", 4, 9, False),
        ("compressing database archive", 4, 9, True),
    ]
