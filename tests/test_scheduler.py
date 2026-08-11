from __future__ import annotations

from feedian.scheduler import _runner_script, _task_names


def test_scheduler_runner_retries_and_supports_if_due(tmp_path) -> None:
    script = _runner_script(tmp_path / "vault")

    assert "for /L %%i in (1,1,6)" in script
    assert "timeout /t 1800" in script
    assert "-m feedian run --vault" in script
    assert "%*" in script


def test_scheduler_task_names_are_per_vault(tmp_path) -> None:
    first = _task_names(tmp_path / "one")
    second = _task_names(tmp_path / "two")

    assert first != second
    assert "periodic" in first[0]
