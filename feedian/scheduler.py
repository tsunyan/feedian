from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .vault import vault_paths


TASK_PREFIX = "Feedian"


@dataclass(frozen=True)
class ScheduleStatus:
    periodic_task: str
    catch_up_task: str
    weekly_exists: bool
    catch_up_exists: bool


def install_schedule(vault_root: str | Path, *, time: str = "03:00") -> ScheduleStatus:
    root = Path(vault_root).resolve()
    paths = vault_paths(root)
    runner = paths.state_dir / "scheduled-run.cmd"
    runner.parent.mkdir(parents=True, exist_ok=True)
    runner.write_text(_runner_script(root), encoding="utf-8", newline="\r\n")
    periodic, catch_up = _task_names(root)
    _run_schtasks(["/Create", "/TN", periodic, "/SC", "HOURLY", "/MO", "6", "/ST", time, "/TR", str(runner), "/F"])
    _run_schtasks(["/Create", "/TN", catch_up, "/SC", "ONLOGON", "/TR", f'"{runner}" --if-due', "/F"])
    return schedule_status(root)


def remove_schedule(vault_root: str | Path) -> ScheduleStatus:
    root = Path(vault_root).resolve()
    periodic, catch_up = _task_names(root)
    for task in (periodic, catch_up):
        result = subprocess.run(["schtasks", "/Delete", "/TN", task, "/F"], text=True, capture_output=True, check=False)
        if result.returncode not in {0, 1}:
            raise RuntimeError((result.stderr or result.stdout or "Could not remove scheduled task.").strip())
    return schedule_status(root)


def schedule_status(vault_root: str | Path) -> ScheduleStatus:
    root = Path(vault_root).resolve()
    periodic, catch_up = _task_names(root)
    return ScheduleStatus(periodic, catch_up, _task_exists(periodic), _task_exists(catch_up))


def _runner_script(vault_root: Path) -> str:
    python = Path(sys.executable).resolve()
    command = f'"{python}" -m feedian run --vault "{vault_root}" %*'
    return (
        "@echo off\n"
        "setlocal\n"
        "for /L %%i in (1,1,6) do (\n"
        f"  {command}\n"
        "  if not errorlevel 1 exit /b 0\n"
        "  if %%i==6 exit /b 1\n"
        "  timeout /t 1800 /nobreak >nul\n"
        ")\n"
        "exit /b 1\n"
    )


def _task_names(root: Path) -> tuple[str, str]:
    # A path-derived suffix permits multiple private Vaults without global task collisions.
    safe = "".join(char if char.isalnum() else "-" for char in str(root)).strip("-")[-48:]
    return f"{TASK_PREFIX} periodic {safe}", f"{TASK_PREFIX} catch-up {safe}"


def _task_exists(task: str) -> bool:
    return subprocess.run(["schtasks", "/Query", "/TN", task], text=True, capture_output=True, check=False).returncode == 0


def _run_schtasks(args: list[str]) -> None:
    try:
        subprocess.run(["schtasks", *args], text=True, capture_output=True, check=True)
    except FileNotFoundError as exc:
        raise RuntimeError("Windows Task Scheduler (schtasks) was not found.") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError((exc.stderr or exc.stdout or "Task Scheduler command failed.").strip()) from exc
