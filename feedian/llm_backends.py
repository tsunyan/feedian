from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from .extract import PageFetchResult
from .llm import (
    LLMAuthError,
    LLMProtocolError,
    LLMRateLimitError,
    LLMServiceError,
    LLMUnavailableError,
    PROVIDER_OUTPUT_SCHEMA,
    SummaryAudit,
    build_summary_request,
    build_untrusted_message,
    normalize_summary_result,
    summarize_bookmark_with_audit,
)
from .local_agent import (
    LocalAgentResult,
    LocalAgentProcessError,
    ProcessRunner,
    SubprocessRunner,
    run_isolated_local_agent,
)


BACKEND_IDS = ("openai-responses", "manus-api", "codex-local", "claude-code-local")
BACKEND_ALIASES = {"openai": "openai-responses", "manus": "manus-api"}
BACKEND_IMPLEMENTATION_REVISION = "llm-backends-v2"


class BackendError(RuntimeError):
    fallback_eligible = False

    def __init__(self, message: str, *, request: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.request = request


class BackendAuthError(BackendError):
    pass


class BackendPolicyError(BackendError):
    pass


class BackendUnavailableError(BackendError):
    pass


class BackendExecutionError(BackendError):
    pass


class BackendTimeoutError(BackendError):
    pass


class BackendRateLimitError(BackendError):
    pass


class BackendProtocolError(BackendError):
    pass


@dataclass(frozen=True)
class BackendCapabilities:
    backend: str
    auth_mode: str
    billing_mode: str
    max_article_chars: int
    usage_available: bool
    max_parallelism: int = 1
    min_start_interval_seconds: float = 0.0


@dataclass(frozen=True)
class BackendAudit:
    result: dict[str, Any]
    request: dict[str, Any]
    response: dict[str, Any]
    usage: dict[str, int]
    auth_mode: str
    billing_mode: str
    metadata: dict[str, Any] = field(default_factory=dict)


class LLMBackend(Protocol):
    capabilities: BackendCapabilities

    def default_model(self) -> str: ...

    def supports_model(self, model: str) -> bool: ...

    def preflight(self) -> dict[str, Any]: ...

    def summarize(
        self,
        *,
        model: str,
        item: dict[str, Any],
        page: PageFetchResult,
        language: str,
        timeout_seconds: int,
        max_output_tokens: int,
        reasoning_effort: str,
        max_retries: int,
        retry_base_seconds: float,
        temporary_parent: Path,
    ) -> BackendAudit: ...


class ApiBackend:
    def __init__(
        self,
        *,
        backend: str,
        provider: str,
        api_key_name: str,
        model_name: str,
        max_article_chars: int,
        usage_available: bool,
        min_start_interval_seconds: float = 0.0,
    ) -> None:
        self.provider = provider
        self.api_key_name = api_key_name
        self.model_name = model_name
        self.capabilities = BackendCapabilities(
            backend=backend,
            auth_mode="api-key",
            billing_mode="metered-api",
            max_article_chars=max_article_chars,
            usage_available=usage_available,
            min_start_interval_seconds=min_start_interval_seconds,
        )
        self._api_key: str | None = None

    def default_model(self) -> str:
        return self.model_name

    def supports_model(self, model: str) -> bool:
        return model.startswith("manus-") == (self.provider == "manus")

    def preflight(self) -> dict[str, Any]:
        api_key = os.environ.get(self.api_key_name, "").strip()
        if not api_key:
            raise BackendAuthError(f"Missing required environment variable: {self.api_key_name}")
        self._api_key = api_key
        return {"implementation_revision": BACKEND_IMPLEMENTATION_REVISION}

    def summarize(
        self,
        *,
        model: str,
        item: dict[str, Any],
        page: PageFetchResult,
        language: str,
        timeout_seconds: int,
        max_output_tokens: int,
        reasoning_effort: str,
        max_retries: int,
        retry_base_seconds: float,
        temporary_parent: Path,
    ) -> BackendAudit:
        del temporary_parent
        if self._api_key is None:
            self.preflight()
        try:
            audit: SummaryAudit = summarize_bookmark_with_audit(
                self._api_key or "",
                model,
                item,
                page,
                language,
                timeout_seconds,
                max_output_tokens,
                reasoning_effort,
                max_retries,
                retry_base_seconds,
                self.capabilities.max_article_chars,
                provider=self.provider,
            )
        except LLMAuthError as exc:
            raise BackendAuthError(str(exc)) from exc
        except LLMRateLimitError as exc:
            raise BackendRateLimitError(str(exc)) from exc
        except LLMUnavailableError as exc:
            raise BackendUnavailableError(str(exc)) from exc
        except LLMProtocolError as exc:
            raise BackendProtocolError(str(exc)) from exc
        except LLMServiceError as exc:
            raise BackendExecutionError(str(exc)) from exc
        except BackendError:
            raise
        except Exception as exc:
            raise BackendExecutionError(str(exc)) from exc
        return BackendAudit(
            result=audit.result,
            request=audit.request,
            response=audit.response,
            usage=audit.usage,
            auth_mode=self.capabilities.auth_mode,
            billing_mode=self.capabilities.billing_mode,
            metadata={"implementation_revision": BACKEND_IMPLEMENTATION_REVISION},
        )


# Codex enables its tools by default and offers no single switch to turn them all
# off, so isolation rests on naming every one. Measured against codex-cli 0.147.0
# on 2026-08-17: with only --sandbox read-only, a direct instruction to read a file
# outside the working directory succeeded (the agent ran pwsh and returned the
# contents). With this list, the same instruction answers that it cannot read
# files. See docs/reviews/20260816-llm-backends-implementation.ja.md.
CODEX_DISABLED_FEATURES = (
    "shell_tool",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "computer_use",
    "in_app_browser",
    "plugins",
    "remote_plugin",
    "plugin_sharing",
    "skill_search",
    "skill_mcp_dependency_install",
    "multi_agent",
    "view_image",
    "image_generation",
    "apps",
    "hooks",
    "tool_suggest",
    "tool_call_mcp_elicitation",
    "code_mode_host",
    "workspace_dependencies",
    "goals",
    "memories",
)
# A denylist only holds for versions it was measured against: a release that adds
# another default-on tool would reopen the surface silently. Re-run the check in
# the review document before adding a version here.
CODEX_VERIFIED_VERSIONS = ("0.147.0",)


class CodexLocalBackend:
    capabilities = BackendCapabilities(
        backend="codex-local",
        auth_mode="local-session",
        billing_mode="subscription",
        max_article_chars=10_000,
        usage_available=True,
    )

    def __init__(
        self,
        *,
        runner: ProcessRunner | None = None,
        executable: str = "codex",
        version: str = "",
        control_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self.runner = runner or SubprocessRunner()
        self.executable = executable
        self.version = version
        self.control_runner = control_runner or subprocess.run
        self._resolved_executable = ""
        self._preflight_metadata: dict[str, Any] | None = None

    def _resolve_executable(self) -> str:
        """Resolve the name on PATH once and use the result everywhere after.

        On Windows the CLI is an npm shim, so PATH holds `codex.CMD`; launching the
        bare name fails because CreateProcess does not apply PATHEXT itself.
        """
        if self._resolved_executable:
            return self._resolved_executable
        resolved = shutil.which(self.executable)
        if resolved is None:
            raise BackendUnavailableError(f"Codex CLI was not found: {self.executable}")
        self._resolved_executable = resolved
        return resolved

    def default_model(self) -> str:
        return "gpt-5.6-terra"

    def supports_model(self, model: str) -> bool:
        return not model.startswith("manus-")

    def preflight(self) -> dict[str, Any]:
        if self._preflight_metadata is not None:
            return dict(self._preflight_metadata)
        version = self.version or self._detect_version()
        if version not in CODEX_VERIFIED_VERSIONS:
            raise BackendPolicyError(
                f"codex-local requires a Codex CLI version whose tool isolation has been "
                f"measured; {version!r} has not been. Verified: "
                f"{', '.join(CODEX_VERIFIED_VERSIONS)}."
            )
        self.version = version
        self._verify_login()
        self._preflight_metadata = {
            "implementation_revision": BACKEND_IMPLEMENTATION_REVISION,
            "cli_version": version,
            "disabled_features": list(CODEX_DISABLED_FEATURES),
        }
        return dict(self._preflight_metadata)

    def _control_executable(self) -> str:
        if isinstance(self.runner, SubprocessRunner):
            return self._resolve_executable()
        return self.executable

    def _verify_login(self) -> None:
        try:
            completed = self.control_runner(
                [self._control_executable(), "login", "status"],
                capture_output=True, text=True, timeout=30, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise BackendUnavailableError(f"Could not check Codex CLI login status: {exc}") from exc
        if completed.returncode != 0:
            raise BackendAuthError("Codex CLI is not logged in; run `codex login` first.")

    def _detect_version(self) -> str:
        """Detect the CLI version once per run; callers cache it on the instance."""
        try:
            completed = self.control_runner(
                [self._resolve_executable(), "--version"],
                capture_output=True, text=True, timeout=30, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise BackendUnavailableError(f"Could not run the Codex CLI: {exc}") from exc
        if completed.returncode != 0:
            raise BackendUnavailableError(
                f"Codex CLI version check exited with status {completed.returncode}."
            )
        match = re.search(r"(\d+\.\d+\.\d+)", completed.stdout)
        if match is None:
            raise BackendUnavailableError("Could not read a version from the Codex CLI.")
        return match.group(1)

    def summarize(
        self,
        *,
        model: str,
        item: dict[str, Any],
        page: PageFetchResult,
        language: str,
        timeout_seconds: int,
        max_output_tokens: int,
        reasoning_effort: str,
        max_retries: int,
        retry_base_seconds: float,
        temporary_parent: Path,
    ) -> BackendAudit:
        del max_retries, retry_base_seconds
        metadata = self.preflight()
        planned = build_summary_request(
            model=model,
            item=item,
            page=page,
            language=language,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
            max_article_chars=self.capabilities.max_article_chars,
        )
        prompt = build_untrusted_message(str(planned["input"][0]["content"][0]["text"]))

        def command(schema_path: Path) -> tuple[str, ...]:
            disables: tuple[str, ...] = ()
            for feature in CODEX_DISABLED_FEATURES:
                disables += ("--disable", feature)
            return (
                self._resolve_executable() if isinstance(self.runner, SubprocessRunner)
                else self.executable,
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--config",
                "mcp_servers={}",
                "--json",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                *disables,
                "--model",
                model,
                "--output-schema",
                str(schema_path),
                "-",
            )

        audit_argv = list(command(Path("<temporary>") / "output-schema.json"))
        audit_argv[0] = Path(audit_argv[0]).name
        audit_request = {"mode": "stdin", "argv": audit_argv}

        try:
            local = run_isolated_local_agent(
                runner=self.runner,
                command=command,
                parse=parse_codex_events,
                stdin_text=prompt,
                output_schema=PROVIDER_OUTPUT_SCHEMA,
                temporary_parent=temporary_parent,
                timeout_seconds=timeout_seconds,
            )
            result = normalize_summary_result(local.result)
        except subprocess.TimeoutExpired as exc:
            raise BackendTimeoutError(
                f"codex-local exceeded {timeout_seconds}s.", request=audit_request,
            ) from exc
        except LocalAgentProcessError as exc:
            raise _classify_codex_process_error(exc, audit_request) from exc
        except BackendError as exc:
            if exc.request is None:
                exc.request = audit_request
            raise
        except Exception as exc:
            raise BackendExecutionError(str(exc), request=audit_request) from exc
        return BackendAudit(
            result=result,
            # The audit copy omits machine-specific paths; the article goes over stdin.
            request=audit_request,
            response={"final_response": result},
            usage=local.usage,
            auth_mode=self.capabilities.auth_mode,
            billing_mode=self.capabilities.billing_mode,
            metadata=metadata,
        )


def _classify_codex_process_error(
    error: LocalAgentProcessError, request: dict[str, Any],
) -> BackendError:
    detail = f"{error.result.stderr}\n{error.result.stdout}".lower()
    message = f"Codex CLI exited with status {error.result.returncode}."
    if any(marker in detail for marker in ("not logged in", "unauthorized", "authentication", "login required")):
        return BackendAuthError(message, request=request)
    if any(marker in detail for marker in ("rate limit", "usage limit", "quota", "too many requests", "429")):
        return BackendRateLimitError(message, request=request)
    if any(marker in detail for marker in ("connection", "network", "service unavailable", "temporarily unavailable", "503")):
        return BackendUnavailableError(message, request=request)
    return BackendExecutionError(message, request=request)


class ClaudeCodeLocalBackend:
    capabilities = BackendCapabilities(
        backend="claude-code-local",
        auth_mode="local-session",
        billing_mode="subscription",
        max_article_chars=10_000,
        usage_available=False,
    )

    def default_model(self) -> str:
        return ""

    def supports_model(self, model: str) -> bool:
        del model
        return True

    def preflight(self) -> dict[str, Any]:
        raise BackendPolicyError(
            "claude-code-local is reserved but unavailable until its CLI contract and isolation policy are verified."
        )

    def summarize(self, **_kwargs: Any) -> BackendAudit:
        self.preflight()
        raise AssertionError("unreachable")


def canonical_backend_id(value: str) -> str:
    backend = BACKEND_ALIASES.get(value, value)
    if backend not in BACKEND_IDS:
        raise ValueError(f"Unsupported LLM backend: {value}")
    return backend


def get_backend(value: str) -> LLMBackend:
    backend = canonical_backend_id(value)
    if backend == "openai-responses":
        return ApiBackend(
            backend=backend,
            provider="openai",
            api_key_name="OPENAI_API_KEY",
            model_name=os.environ.get("OPENAI_MODEL", "gpt-5.6-terra"),
            max_article_chars=10_000,
            usage_available=True,
        )
    if backend == "manus-api":
        return ApiBackend(
            backend=backend,
            provider="manus",
            api_key_name="MANUS_API_KEY",
            model_name=os.environ.get("MANUS_MODEL", "manus-1.6"),
            max_article_chars=3_000,
            usage_available=False,
            # Manus pacing lives in llm.py because the legacy export path shares
            # that call; declaring it here too would make each create wait twice.
            min_start_interval_seconds=0.0,
        )
    if backend == "codex-local":
        return CodexLocalBackend()
    return ClaudeCodeLocalBackend()


def parse_codex_events(stdout: str) -> LocalAgentResult:
    """Read Codex's JSONL event stream. The format belongs to this adapter."""
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
        raise BackendProtocolError("Codex did not return a final structured response.")
    try:
        result = json.loads(final_text)
    except json.JSONDecodeError as exc:
        raise BackendProtocolError("Codex final response was not valid JSON.") from exc
    if not isinstance(result, dict):
        raise BackendProtocolError("Codex final response was not a JSON object.")
    return LocalAgentResult(result=result, usage=usage)
