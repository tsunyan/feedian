from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .extract import AllowAllHosts
from .vault import FetchPolicy, NetworkPolicy, VaultConfig


@dataclass
class Config:
    vault_path: str
    output_folder: str = "Raindrop"
    hatena_input: str = ""
    hatena_output_folder: str = "Hatena"
    hatena_base_tags: list[str] | None = None
    hatena_request_interval_seconds: float = 0.3
    hatena_fetch_public_comments: bool = True
    collection_id: int = 0
    nested: bool = True
    per_page: int = 50
    base_tags: list[str] | None = None
    language: str = "ja"
    openai_model: str = "gpt-5.6-luna"
    max_article_chars: int = 10000
    max_output_tokens: int = 800
    openai_reasoning_effort: str = "none"
    allow_private_urls: bool = False
    max_retries: int = 3
    retry_base_seconds: float = 1.0
    request_timeout_seconds: int = 30
    sleep_seconds: float = 0.3
    sync_request_interval_seconds: float = 0.5

    def __post_init__(self) -> None:
        if self.base_tags is None:
            self.base_tags = ["raindrop", "bookmark"]
        if self.hatena_base_tags is None:
            self.hatena_base_tags = ["hatena", "bookmark"]
        self.per_page = max(1, min(int(self.per_page), 50))
        self.max_article_chars = max(1000, int(self.max_article_chars))
        self.max_output_tokens = max(100, int(self.max_output_tokens))
        if self.openai_reasoning_effort not in {"none", "minimal", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError("openai_reasoning_effort must be a supported reasoning effort.")
        self.max_retries = max(0, min(int(self.max_retries), 5))
        self.retry_base_seconds = max(0.1, float(self.retry_base_seconds))
        self.request_timeout_seconds = max(1, int(self.request_timeout_seconds))
        self.sleep_seconds = max(0.0, float(self.sleep_seconds))
        self.sync_request_interval_seconds = max(0.0, float(self.sync_request_interval_seconds))
        self.hatena_request_interval_seconds = max(0.0, float(self.hatena_request_interval_seconds))

    def model_copy(self) -> "Config":
        return replace(
            self,
            base_tags=list(self.base_tags or []),
            hatena_base_tags=list(self.hatena_base_tags or []),
        )


def fetch_policy_from_config(config: Config) -> FetchPolicy:
    """Adapt the legacy `--source hatena`-style Config to a FetchPolicy.

    This is the only path Config feeds into fetch_page_text through; it keeps
    today's CLI behavior unchanged rather than growing a host allow-list
    concept Config was never designed to carry. html_max_bytes/document_max_bytes/
    browser_timeout_seconds have no Config equivalent, so this borrows
    VaultConfig's defaults, matching what every caller already shared before
    fetch.html_max_bytes etc. were wired up.
    """
    defaults = VaultConfig().fetch
    allowed_private_hosts: frozenset[str] = AllowAllHosts() if config.allow_private_urls else frozenset()
    return FetchPolicy(
        network=NetworkPolicy(allowed_private_hosts=allowed_private_hosts),
        html_max_bytes=defaults["html_max_bytes"],
        document_max_bytes=defaults["document_max_bytes"],
        timeout_seconds=config.request_timeout_seconds,
        browser_timeout_seconds=defaults["browser_timeout_seconds"],
    )


def load_config(path: str | Path) -> Config:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    data = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Config JSON must be an object.")
    return Config(**_known_keys(data))


def _known_keys(data: dict[str, Any]) -> dict[str, Any]:
    allowed = set(Config.__dataclass_fields__.keys())
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ValueError(f"Unknown config field(s): {', '.join(unknown)}")
    return dict(data)
