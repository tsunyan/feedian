from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from typing import TextIO

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    Task,
    TaskID,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.text import Text


PROGRESS_MODES = ("auto", "rich", "plain", "off")


class _KnownOrUnknownTotalColumn(MofNCompleteColumn):
    def render(self, task: Task) -> Text:
        if task.total is None:
            return Text(f"{int(task.completed)}/???", style="progress.download")
        if task.fields.get("estimated_total"):
            return Text(f"{int(task.completed)}/~{int(task.total)}", style="progress.download")
        return super().render(task)


@dataclass
class _PlainTask:
    description: str
    total: int | None
    estimated_total: bool = False
    completed: int = 0
    started_at: float = 0.0
    last_reported_at: float = 0.0


class ProgressReporter:
    def __init__(
        self,
        requested_mode: str,
        *,
        verbose: bool = False,
        stream: TextIO | None = None,
        plain_interval_seconds: float = 5.0,
    ) -> None:
        if requested_mode not in PROGRESS_MODES:
            raise ValueError(f"unsupported progress mode: {requested_mode}")
        self.stream = stream or sys.stdout
        self.verbose = verbose
        self.mode = self._resolve_mode(requested_mode)
        self.plain_interval_seconds = plain_interval_seconds
        self._console: Console | None = None
        self._progress: Progress | None = None
        self._rich_task_id: TaskID | None = None
        self._plain_task: _PlainTask | None = None

    def __enter__(self) -> ProgressReporter:
        if self.mode == "rich":
            self._console = Console(file=self.stream, force_terminal=True)
            self._progress = Progress(
                SpinnerColumn(),
                TextColumn("{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                _KnownOrUnknownTotalColumn(),
                TimeElapsedColumn(),
                TimeRemainingColumn(),
                console=self._console,
                transient=False,
            )
            self._progress.start()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if self._progress is not None:
            self._progress.stop()

    def start_task(
        self,
        description: str,
        *,
        total: int | None = None,
        estimated_total: bool = False,
    ) -> None:
        if self.mode == "off":
            return
        if self.mode == "rich":
            assert self._progress is not None
            if self._rich_task_id is not None:
                self._progress.remove_task(self._rich_task_id)
            self._rich_task_id = self._progress.add_task(
                description,
                total=total,
                estimated_total=estimated_total,
            )
            return
        now = time.monotonic()
        self._plain_task = _PlainTask(
            description=description,
            total=total,
            estimated_total=estimated_total,
            started_at=now,
            last_reported_at=now,
        )
        self._write(self._plain_text(self._plain_task))

    def advance(self, amount: int = 1) -> None:
        if self.mode == "off":
            return
        if self.mode == "rich":
            if self._progress is not None and self._rich_task_id is not None:
                self._progress.advance(self._rich_task_id, amount)
            return
        if self._plain_task is None:
            return
        self._plain_task.completed += amount
        now = time.monotonic()
        complete = (
            self._plain_task.total is not None
            and not self._plain_task.estimated_total
            and self._plain_task.completed >= self._plain_task.total
        )
        if complete or now - self._plain_task.last_reported_at >= self.plain_interval_seconds:
            self._write(self._plain_text(self._plain_task))
            self._plain_task.last_reported_at = now

    def log(self, message: str) -> None:
        if self.mode == "off":
            return
        if self._console is not None:
            self._console.print(message)
            return
        self._write(message)

    def verbose_log(self, message: str) -> None:
        if self.verbose:
            self.log(message)

    def _resolve_mode(self, requested_mode: str) -> str:
        if requested_mode != "auto":
            return requested_mode
        try:
            return "rich" if self.stream.isatty() else "plain"
        except (AttributeError, OSError):
            return "plain"

    def _plain_text(self, task: _PlainTask) -> str:
        elapsed = max(0.0, time.monotonic() - task.started_at)
        if task.total is None:
            return f"{task.description} processed={task.completed} elapsed={_format_duration(elapsed)}"
        total = f"~{task.total}" if task.estimated_total else str(task.total)
        text = f"{task.description} {task.completed}/{total} elapsed={_format_duration(elapsed)}"
        if task.completed <= 0 or elapsed <= 0:
            return text
        rate = task.completed / elapsed
        remaining = max(0, task.total - task.completed) / rate
        return f"{text} rate={rate:.1f}/s eta={_format_duration(remaining)}"

    def _write(self, message: str) -> None:
        print(message, file=self.stream, flush=True)


def _format_duration(seconds: float) -> str:
    total_seconds = max(0, round(seconds))
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
