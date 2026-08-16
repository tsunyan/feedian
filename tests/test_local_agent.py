from __future__ import annotations

import subprocess
import sys
import textwrap
import time

import pytest

from feedian.local_agent import (
    LocalAgentProcessError,
    ProcessResult,
    SubprocessRunner,
    minimal_child_environment,
    sanitized_argv,
)


def test_timeout_kills_the_grandchild_not_only_the_process_it_started(tmp_path) -> None:
    """A local agent starts its own children, and they keep spending tokens.

    The fake-runner contract tests cannot show this: only a real tree can.
    """

    marker = tmp_path / "grandchild-survived.txt"
    grandchild = textwrap.dedent(
        f"""
        import time
        time.sleep(4)
        open(r"{marker}", "w").write("alive")
        """
    )
    parent = textwrap.dedent(
        f"""
        import subprocess, sys, time
        subprocess.Popen([sys.executable, "-c", {grandchild!r}])
        time.sleep(60)
        """
    )

    with pytest.raises(subprocess.TimeoutExpired):
        SubprocessRunner().run(
            [sys.executable, "-c", parent],
            stdin_text="",
            cwd=tmp_path,
            timeout_seconds=1.5,
            env=minimal_child_environment(),
        )

    # Outlive the grandchild's own delay; if the tree survived, it writes now.
    time.sleep(5)
    assert not marker.exists(), "the grandchild outlived the timeout"


def test_process_error_quotes_diagnostics_but_never_the_agent_output() -> None:
    result = ProcessResult(1, stdout="SECRET-ARTICLE-TEXT rate limit", stderr="quota exceeded")

    error = LocalAgentProcessError(result, ("codex", "exec"))

    assert "quota exceeded" in str(error)
    assert "SECRET-ARTICLE-TEXT" not in str(error)
    assert error.diagnostics == "quota exceeded"


def test_sanitized_argv_keeps_the_flags_and_drops_the_machine_paths(tmp_path) -> None:
    argv = (str(tmp_path / "bin" / "codex.CMD"), "exec", "--output-schema",
            str(tmp_path / "output-schema.json"))

    sanitized = sanitized_argv(argv, tmp_path)

    assert sanitized[0] == "codex.CMD"
    assert sanitized[1:3] == ("exec", "--output-schema")
    assert sanitized[3].startswith("<temporary>")
    assert sanitized[3].endswith("output-schema.json")
    assert str(tmp_path) not in " ".join(sanitized)


def test_allowlisted_environment_excludes_provider_keys(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("PATH", "/usr/bin")

    environment = minimal_child_environment(CODEX_HOME="/tmp/home")

    assert environment["PATH"] == "/usr/bin"
    assert environment["CODEX_HOME"] == "/tmp/home"
    assert "OPENAI_API_KEY" not in environment
