from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Protocol, Sequence


MAX_ERROR_BYTES = 8 * 1024


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


class ProcessRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        stdin_text: str,
        cwd: Path,
        timeout_seconds: float,
    ) -> ProcessResult: ...


class SubprocessRunner:
    def run(
        self,
        argv: Sequence[str],
        *,
        stdin_text: str,
        cwd: Path,
        timeout_seconds: float,
    ) -> ProcessResult:
        """Run one agent process, terminating its whole tree on timeout.

        `subprocess.run` kills only the process it started, which would leave a
        local agent's own children running and still spending model tokens after
        Feedian gave up on them.
        """
        popen_kwargs: dict[str, object] = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        process = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
            **popen_kwargs,  # type: ignore[arg-type]
        )
        try:
            stdout, stderr = process.communicate(stdin_text, timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            terminate_process_tree(process)
            process.communicate()
            raise
        return ProcessResult(process.returncode, stdout, stderr)


def terminate_process_tree(process: subprocess.Popen[str]) -> None:
    """Kill a process and everything it started."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        # Windows has no process groups that survive an intermediate exit, so ask
        # the OS to walk the tree by PID.
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            check=False,
        )
        if process.poll() is None:
            process.kill()
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        process.kill()


@dataclass(frozen=True)
class LocalAgentResult:
    result: dict[str, object]
    usage: dict[str, int]
    argv: tuple[str, ...] = ()


def run_isolated_local_agent(
    *,
    runner: ProcessRunner,
    command: Callable[[Path], Sequence[str]],
    parse: Callable[[str], LocalAgentResult],
    stdin_text: str,
    output_schema: dict[str, object],
    temporary_parent: Path,
    timeout_seconds: float,
) -> LocalAgentResult:
    """Run one local-agent process without placing untrusted input in argv.

    The caller supplies the command builder and the event parser, because flags
    and event formats belong to each CLI rather than to this runner. Only the
    schema path is exposed to the child; the article and prompt are delivered
    exclusively through stdin.
    """

    temporary_parent = temporary_parent.resolve()
    temporary_parent.mkdir(parents=True, exist_ok=True)
    temporary_path = Path(tempfile.mkdtemp(prefix="llm-", dir=temporary_parent)).resolve()
    if temporary_parent not in temporary_path.parents:
        raise RuntimeError("Temporary local-agent directory escaped its configured parent.")
    try:
        if os.name != "nt":
            temporary_path.chmod(0o700)
        schema_path = temporary_path / "output-schema.json"
        schema_path.write_text(
            json.dumps(output_schema, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        if os.name != "nt":
            schema_path.chmod(0o600)
        argv = tuple(str(value) for value in command(schema_path))
        if stdin_text and any(stdin_text in argument for argument in argv):
            raise RuntimeError("Untrusted local-agent input must not appear in argv.")
        completed = runner.run(
            argv,
            stdin_text=stdin_text,
            cwd=temporary_path,
            timeout_seconds=timeout_seconds,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"Local agent exited with status {completed.returncode}.")
        parsed = parse(completed.stdout)
        return replace(parsed, argv=argv)
    finally:
        shutil.rmtree(temporary_path, ignore_errors=True)


def sanitize_error(
    value: str, *private_paths: Path, private_values: tuple[str, ...] = ()
) -> str:
    sanitized = value
    for path in private_paths:
        rendered = str(path)
        if rendered:
            sanitized = sanitized.replace(rendered, "<redacted-path>")
    for private_value in private_values:
        if private_value:
            sanitized = sanitized.replace(private_value, "<redacted-content>")
    sanitized = re.sub(
        r"(?i)(Authorization:\s*Bearer\s+|Bearer\s+|x-manus-api-key:\s*)\S+",
        lambda match: f"{match.group(1)}<redacted>",
        sanitized,
    )
    encoded = sanitized.strip().encode("utf-8")[:MAX_ERROR_BYTES]
    return encoded.decode("utf-8", errors="ignore")
