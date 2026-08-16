from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
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
        completed = subprocess.run(
            list(argv),
            input=stdin_text,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            cwd=cwd,
            timeout=timeout_seconds,
            check=False,
        )
        return ProcessResult(completed.returncode, completed.stdout, completed.stderr)


@dataclass(frozen=True)
class LocalAgentResult:
    result: dict[str, object]
    usage: dict[str, int]


def run_isolated_local_agent(
    *,
    runner: ProcessRunner,
    command: Callable[[Path, Path], Sequence[str]],
    stdin_text: str,
    output_schema: dict[str, object],
    temporary_parent: Path,
    timeout_seconds: float,
) -> LocalAgentResult:
    """Run one local-agent process without placing untrusted input in argv.

    The caller supplies a command builder so contract tests can verify every
    argument. Only the schema and final-message paths are exposed to the child;
    the article and prompt are delivered exclusively through stdin.
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
        final_path = temporary_path / "final-response.json"
        schema_path.write_text(
            json.dumps(output_schema, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        if os.name != "nt":
            schema_path.chmod(0o600)
        argv = tuple(str(value) for value in command(schema_path, final_path))
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
        return _parse_codex_jsonl(completed.stdout)
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


def _parse_codex_jsonl(stdout: str) -> LocalAgentResult:
    final_text = ""
    usage: dict[str, int] = {}
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str):
                    final_text = text
        if event.get("type") == "turn.completed":
            raw_usage = event.get("usage")
            if isinstance(raw_usage, dict):
                for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
                    value = raw_usage.get(key)
                    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                        usage[key] = value
    if not final_text:
        raise RuntimeError("Local agent did not return a final structured response.")
    try:
        result = json.loads(final_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Local agent final response was not valid JSON.") from exc
    if not isinstance(result, dict):
        raise RuntimeError("Local agent final response was not a JSON object.")
    return LocalAgentResult(result=result, usage=usage)
