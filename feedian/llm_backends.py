from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .extract import PageFetchResult
from .llm import (
    CANONICAL_SUMMARY_SCHEMA,
    SummaryAudit,
    build_summary_request,
    build_untrusted_message,
    normalize_summary_result,
    summarize_bookmark_with_audit,
)
from .local_agent import ProcessRunner, SubprocessRunner, run_isolated_local_agent


BACKEND_IDS = ("openai-responses", "manus-api", "codex-local", "claude-code-local")
BACKEND_ALIASES = {"openai": "openai-responses", "manus": "manus-api"}
BACKEND_IMPLEMENTATION_REVISION = "llm-backends-v1"


class BackendError(RuntimeError):
    fallback_eligible = False


class BackendAuthError(BackendError):
    pass


class BackendPolicyError(BackendError):
    pass


class BackendUnavailableError(BackendError):
    pass


class BackendExecutionError(BackendError):
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
        if self.provider == "manus" and not model.startswith("manus-"):
            raise BackendUnavailableError(
                f"Model {model!r} is not supported by backend {self.capabilities.backend}."
            )
        if self.provider == "openai" and model.startswith("manus-"):
            raise BackendUnavailableError(
                f"Model {model!r} is not supported by backend {self.capabilities.backend}."
            )
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
        policy_verified: bool = False,
        version: str = "",
    ) -> None:
        self.runner = runner or SubprocessRunner()
        self.executable = executable
        self.policy_verified = policy_verified
        self.version = version

    def default_model(self) -> str:
        return "gpt-5.6-terra"

    def preflight(self) -> dict[str, Any]:
        if not self.policy_verified:
            raise BackendPolicyError(
                "codex-local is unavailable: the Codex CLI read-only sandbox does not prove that "
                "shell and reads outside the temporary directory are disabled."
            )
        if shutil.which(self.executable) is None and isinstance(self.runner, SubprocessRunner):
            raise BackendUnavailableError(f"Codex CLI was not found: {self.executable}")
        return {
            "implementation_revision": BACKEND_IMPLEMENTATION_REVISION,
            "cli_version": self.version or "unknown",
            "policy": "verified-test-runner",
        }

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
        if model.startswith("manus-"):
            raise BackendUnavailableError(f"Model {model!r} is not supported by codex-local.")
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

        def command(schema_path: Path, _final_path: Path) -> tuple[str, ...]:
            return (
                self.executable,
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--json",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--model",
                model,
                "--output-schema",
                str(schema_path),
                "-",
            )

        try:
            local = run_isolated_local_agent(
                runner=self.runner,
                command=command,
                stdin_text=prompt,
                output_schema=CANONICAL_SUMMARY_SCHEMA,
                temporary_parent=temporary_parent,
                timeout_seconds=timeout_seconds,
            )
            result = normalize_summary_result(local.result)
        except Exception as exc:
            raise BackendExecutionError(str(exc)) from exc
        return BackendAudit(
            result=result,
            request={"mode": "stdin", "schema": "canonical-summary-v1"},
            response={"final_response": result},
            usage=local.usage,
            auth_mode=self.capabilities.auth_mode,
            billing_mode=self.capabilities.billing_mode,
            metadata=metadata,
        )


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
            min_start_interval_seconds=6.1,
        )
    if backend == "codex-local":
        return CodexLocalBackend()
    return ClaudeCodeLocalBackend()
