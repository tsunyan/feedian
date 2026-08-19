from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


VAULT_CONFIG_RELATIVE_PATH = Path(".feedian") / "config.json"
VAULT_DATABASE_NAME = "feedian.sqlite3"
VAULT_CONFIG_VERSION = 2


@dataclass
class RssFeedSettings:
    url: str
    name: str = ""
    folder: str = ""
    tags: list[str] = field(default_factory=list)
    route: str = ""
    enabled: bool = True


@dataclass
class ProviderSettings:
    folder: str
    enabled: bool = True
    collection_id: int | None = None
    poll_hours: int | None = None
    feeds: list[RssFeedSettings] = field(default_factory=list)
    layout: str = "flat"
    category_routes: dict[str, str] = field(default_factory=dict)


@dataclass
class LLMFallbackSettings:
    enabled: bool = False
    backend: str = ""
    model: str = ""


@dataclass
class LLMSettings:
    backend: str = "openai-responses"
    model: str = "gpt-5.6-terra"
    workers: int = 8
    fallback: LLMFallbackSettings = field(default_factory=LLMFallbackSettings)


@dataclass
class VaultConfig:
    format_version: int = VAULT_CONFIG_VERSION
    raw_folder: str = "raw"
    source_folder: str = "source"
    review_folder: str = "review"
    providers: dict[str, ProviderSettings] = field(
        default_factory=lambda: {
            "raindrop": ProviderSettings(folder="Raindrop", poll_hours=168),
            "hatena": ProviderSettings(folder="Hatena", poll_hours=168),
            "rss": ProviderSettings(folder="RSS", enabled=False, poll_hours=6, layout="feed/year/month"),
        }
    )
    fetch: dict[str, Any] = field(
        default_factory=lambda: {
            "html_max_bytes": 10 * 1024 * 1024,
            "document_max_bytes": 100 * 1024 * 1024,
            "refresh_days": 30,
            "workers": 8,
            "comment_workers": 8,
            "star_refresh_days": 30,
            "allow_private_hosts": [],
            "retry_base_minutes": 30,
            "retry_max_days": 30,
            "terminal_http_statuses": [404, 410],
            "terminal_failure_kinds": ["dns", "timeout"],
            "terminal_kind_failures": 3,
            "timeout_seconds": 5,
            "browser_timeout_seconds": 30,
        }
    )
    llm: LLMSettings = field(default_factory=LLMSettings)

    def provider_output_folder(self, provider: str) -> Path:
        settings = self.providers.get(provider)
        if settings is None:
            raise ValueError(f"Unknown provider in vault config: {provider}")
        return Path(self.raw_folder) / settings.folder


@dataclass(frozen=True)
class VaultPaths:
    root: Path
    config_path: Path
    state_dir: Path
    database_path: Path
    search_database_path: Path
    raw_dir: Path
    source_dir: Path
    review_dir: Path
    logs_dir: Path


def vault_paths(root: str | Path) -> VaultPaths:
    resolved_root = Path(root).expanduser().resolve()
    state_dir = resolved_root / ".feedian"
    return VaultPaths(
        root=resolved_root,
        config_path=resolved_root / VAULT_CONFIG_RELATIVE_PATH,
        state_dir=state_dir,
        database_path=state_dir / VAULT_DATABASE_NAME,
        search_database_path=state_dir / "cache" / "search.sqlite3",
        raw_dir=resolved_root / "raw",
        source_dir=resolved_root / "source",
        review_dir=resolved_root / "review",
        logs_dir=state_dir / "logs",
    )


def user_settings_path() -> Path:
    if os.name == "nt":
        root = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return root / "Feedian" / "settings.json"
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return config_home / "feedian" / "settings.json"


def user_env_path() -> Path:
    """A per-user .env beside the user settings file.

    Vault selection is already independent of the working directory, so
    credentials must be too: a scheduled run starts in an arbitrary directory and
    would otherwise find no .env at all. This location is outside every Vault, so
    it cannot be committed by a Vault's own Git repository.
    """
    return user_settings_path().with_name(".env")


def load_user_settings() -> dict[str, Any]:
    path = user_settings_path()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read Feedian user settings: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("Feedian user settings must be a JSON object.")
    return value


def save_default_vault(root: str | Path) -> Path:
    resolved_root = Path(root).expanduser().resolve()
    settings_path = user_settings_path()
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = settings_path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"default_vault": str(resolved_root)}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(settings_path)
    return settings_path


def find_vault_root(*, explicit: str | None = None, cwd: str | Path | None = None) -> Path:
    if explicit:
        root = Path(explicit).expanduser().resolve()
        if not (root / VAULT_CONFIG_RELATIVE_PATH).is_file():
            raise FileNotFoundError(f"Vault config not found: {root / VAULT_CONFIG_RELATIVE_PATH}")
        return root

    current = Path(cwd or Path.cwd()).expanduser().resolve()
    for candidate in (current, *current.parents):
        if (candidate / VAULT_CONFIG_RELATIVE_PATH).is_file():
            return candidate

    default_vault = load_user_settings().get("default_vault")
    if isinstance(default_vault, str) and default_vault.strip():
        root = Path(default_vault).expanduser().resolve()
        if (root / VAULT_CONFIG_RELATIVE_PATH).is_file():
            return root
        raise FileNotFoundError(f"Default Vault config not found: {root / VAULT_CONFIG_RELATIVE_PATH}")
    raise FileNotFoundError("No Feedian vault found. Pass --vault or run feedian init first.")


def initialize_vault(root: str | Path) -> VaultPaths:
    paths = vault_paths(root)
    if paths.config_path.exists():
        raise FileExistsError(f"Vault config already exists: {paths.config_path}")
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    config = VaultConfig()
    paths.config_path.write_text(render_vault_config(config), encoding="utf-8")
    (paths.state_dir / ".gitignore").write_text(
        "# Local Feedian state; config and snapshot manifest are intentionally tracked.\n"
        "feedian.sqlite3\n"
        "feedian.sqlite3-shm\n"
        "feedian.sqlite3-wal\n"
        "feedian.lock\n"
        "cache/\n"
        "logs/\n"
        "staging/\n"
        "tmp/\n"
        "scheduled-run.cmd\n",
        encoding="utf-8",
    )
    return paths


def load_vault_config(root: str | Path) -> VaultConfig:
    paths = vault_paths(root)
    try:
        raw = json.loads(paths.config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read vault config: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("Vault config must be a JSON object.")
    format_version = int(raw.get("format_version", 1))
    if format_version != VAULT_CONFIG_VERSION:
        if format_version < VAULT_CONFIG_VERSION:
            raise RuntimeError("Vault config migration is required; run `feedian migrate --vault ...`.")
        raise RuntimeError(
            f"Vault config format {format_version} is newer than this Feedian version "
            f"({VAULT_CONFIG_VERSION})."
        )
    allowed = {
        "format_version", "raw_folder", "source_folder", "review_folder", "providers", "fetch", "llm"
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"Unknown vault config field(s): {', '.join(unknown)}")
    providers = _parse_providers(raw.get("providers"))
    fetch = VaultConfig().fetch
    fetch.update(dict(raw.get("fetch") or {}))
    # Checked here so a bad value stops the command before any provider is
    # contacted, rather than partway through a run.
    for key in ("workers", "comment_workers", "quick_stop_after_known_pages"):
        if key in fetch:
            positive_int_setting(f"fetch.{key}", fetch[key])
    return VaultConfig(
        format_version=format_version,
        raw_folder=_relative_folder(raw.get("raw_folder", "raw"), "raw_folder"),
        source_folder=_relative_folder(raw.get("source_folder", "source"), "source_folder"),
        review_folder=_relative_folder(raw.get("review_folder", "review"), "review_folder"),
        providers=providers,
        fetch=fetch,
        llm=_parse_llm(raw.get("llm")),
    )


def render_vault_config(config: VaultConfig) -> str:
    providers = {
        name: {
            "folder": settings.folder,
            "enabled": settings.enabled,
            **({"collection_id": settings.collection_id} if settings.collection_id is not None else {}),
            **({"poll_hours": settings.poll_hours} if settings.poll_hours is not None else {}),
            **({"feeds": [_render_rss_feed(feed) for feed in settings.feeds]} if settings.feeds else {}),
            **({"layout": settings.layout} if settings.layout != "flat" else {}),
            **({"category_routes": settings.category_routes} if settings.category_routes else {}),
        }
        for name, settings in config.providers.items()
    }
    payload = {
        "format_version": config.format_version,
        "raw_folder": config.raw_folder,
        "source_folder": config.source_folder,
        "review_folder": config.review_folder,
        "providers": providers,
        "fetch": config.fetch,
        "llm": {
            "backend": config.llm.backend,
            "model": config.llm.model,
            "workers": config.llm.workers,
            "fallback": {
                "enabled": config.llm.fallback.enabled,
                "backend": config.llm.fallback.backend,
                "model": config.llm.fallback.model,
            },
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def migrate_vault_config(root: str | Path) -> bool:
    """Explicitly migrate a version-one Vault config to version two."""

    path = vault_paths(root).config_path
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read vault config: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("Vault config must be a JSON object.")
    version = int(raw.get("format_version", 1))
    if version > VAULT_CONFIG_VERSION:
        raise RuntimeError(
            f"Vault config format {version} is newer than this Feedian version ({VAULT_CONFIG_VERSION})."
        )
    if version == VAULT_CONFIG_VERSION:
        # Validate rather than silently accepting an invalid current config.
        load_vault_config(root)
        return False
    if version != 1:
        raise RuntimeError(f"No migration path from Vault config format {version}.")
    allowed = {"format_version", "raw_folder", "source_folder", "review_folder", "providers", "fetch"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"Unknown vault config field(s): {', '.join(unknown)}")
    providers = _parse_providers(raw.get("providers"))
    fetch = VaultConfig().fetch
    fetch.update(dict(raw.get("fetch") or {}))
    migrated = VaultConfig(
        raw_folder=_relative_folder(raw.get("raw_folder", "raw"), "raw_folder"),
        source_folder=_relative_folder(raw.get("source_folder", "source"), "source_folder"),
        review_folder=_relative_folder(raw.get("review_folder", "review"), "review_folder"),
        providers=providers,
        fetch=fetch,
    )
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(render_vault_config(migrated), encoding="utf-8")
    temporary.replace(path)
    return True


def _parse_llm(raw: object) -> LLMSettings:
    if raw is None:
        return LLMSettings()
    if not isinstance(raw, dict):
        raise ValueError("llm must be a JSON object.")
    unknown = sorted(set(raw) - {"backend", "model", "workers", "fallback"})
    if unknown:
        raise ValueError(f"Unknown llm field(s): {', '.join(unknown)}")
    # Added inside format version 2. A config written before this key existed
    # takes the default, so no migration is required to open it.
    workers = positive_int_setting("llm.workers", raw.get("workers", LLMSettings.workers))
    backend_value = raw.get("backend", "openai-responses")
    model_value = raw.get("model", "gpt-5.6-terra")
    if not isinstance(backend_value, str) or not isinstance(model_value, str):
        raise ValueError("llm.backend and llm.model must be strings.")
    backend = backend_value.strip()
    model = model_value.strip()
    if not backend or not model:
        raise ValueError("llm.backend and llm.model must be non-empty strings.")
    allowed_backends = {"openai-responses", "manus-api", "codex-local", "claude-code-local"}
    if backend not in allowed_backends:
        raise ValueError(f"Unknown llm.backend: {backend}")
    fallback_raw = raw.get("fallback") or {}
    if not isinstance(fallback_raw, dict):
        raise ValueError("llm.fallback must be a JSON object.")
    fallback_unknown = sorted(set(fallback_raw) - {"enabled", "backend", "model"})
    if fallback_unknown:
        raise ValueError(f"Unknown llm.fallback field(s): {', '.join(fallback_unknown)}")
    enabled = fallback_raw.get("enabled", False)
    fallback_backend = fallback_raw.get("backend", "")
    fallback_model = fallback_raw.get("model", "")
    if not isinstance(enabled, bool):
        raise ValueError("llm.fallback.enabled must be a boolean.")
    if not isinstance(fallback_backend, str) or not isinstance(fallback_model, str):
        raise ValueError("llm.fallback.backend and llm.fallback.model must be strings.")
    fallback = LLMFallbackSettings(
        enabled=enabled,
        backend=fallback_backend.strip(),
        model=fallback_model.strip(),
    )
    if fallback.enabled and (not fallback.backend or not fallback.model):
        raise ValueError("Enabled llm.fallback requires both backend and model.")
    if fallback.backend and fallback.backend not in allowed_backends:
        raise ValueError(f"Unknown llm.fallback.backend: {fallback.backend}")
    return LLMSettings(backend=backend, model=model, workers=workers, fallback=fallback)


def _parse_providers(raw: object) -> dict[str, ProviderSettings]:
    if raw is None:
        return VaultConfig().providers
    if not isinstance(raw, dict):
        raise ValueError("providers must be a JSON object.")
    providers: dict[str, ProviderSettings] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not isinstance(value, dict):
            raise ValueError("Each provider must have an object configuration.")
        allowed = {"folder", "enabled", "collection_id", "poll_hours", "feeds", "layout", "category_routes"}
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ValueError(f"Unknown provider field(s) for {name}: {', '.join(unknown)}")
        folder = _relative_folder(value.get("folder", name.title()), f"providers.{name}.folder")
        collection_id = value.get("collection_id")
        poll_hours = value.get("poll_hours")
        raw_feeds = value.get("feeds", [])
        if not isinstance(raw_feeds, list):
            raise ValueError(f"providers.{name}.feeds must be an array.")
        feeds = [_parse_rss_feed(feed, index=index) for index, feed in enumerate(raw_feeds)]
        default_layout = "feed/year/month" if name == "rss" else "flat"
        layout = str(value.get("layout", default_layout)).strip()
        allowed_layouts = {"flat", "feed", "feed/year", "feed/year/month", "route/feed/year/month"}
        if layout not in allowed_layouts:
            raise ValueError(
                f"providers.{name}.layout must be one of: {', '.join(sorted(allowed_layouts))}."
            )
        raw_routes = value.get("category_routes", {})
        if not isinstance(raw_routes, dict) or not all(
            isinstance(tag, str) and tag.strip() and isinstance(route, str) and route.strip()
            for tag, route in raw_routes.items()
        ):
            raise ValueError(f"providers.{name}.category_routes must map non-empty tags to folders.")
        providers[name] = ProviderSettings(
            folder=folder,
            enabled=bool(value.get("enabled", True)),
            collection_id=int(collection_id) if collection_id is not None else None,
            poll_hours=max(1, int(poll_hours)) if poll_hours is not None else None,
            feeds=feeds,
            layout=layout,
            category_routes={
                str(tag).strip(): _relative_folder(route, f"providers.{name}.category_routes.{tag}")
                for tag, route in raw_routes.items()
            },
        )
    return providers


def positive_int_setting(name: str, value: object) -> int:
    """One definition of the rule for every worker and page-count setting.

    Coercing first would silently turn 1.5 into 1 and true into 1, giving the
    run a concurrency the user never wrote. load_vault_config applies this to a
    user's file; the read sites apply it again because a VaultConfig can also be
    built in code, where nothing has been through the parser.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"config.{name} must be an integer")
    if value < 1:
        raise ValueError(f"config.{name} must be >= 1")
    return value


def _relative_folder(value: object, field_name: str) -> str:
    text = str(value)
    path = Path(text)
    if not text or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{field_name} must be a non-empty relative path.")
    return path.as_posix()


def _parse_rss_feed(value: object, *, index: int) -> RssFeedSettings:
    field_name = f"providers.rss.feeds[{index}]"
    if isinstance(value, str):
        url = value.strip()
        if not url:
            raise ValueError(f"{field_name} must not be empty.")
        return RssFeedSettings(url=url)
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a URL string or an object.")
    allowed = {"url", "name", "folder", "tags", "route", "enabled"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"Unknown RSS feed field(s) in {field_name}: {', '.join(unknown)}")
    url = str(value.get("url") or "").strip()
    if not url:
        raise ValueError(f"{field_name}.url must not be empty.")
    raw_tags = value.get("tags", [])
    if not isinstance(raw_tags, list) or not all(isinstance(tag, str) and tag.strip() for tag in raw_tags):
        raise ValueError(f"{field_name}.tags must be an array of non-empty strings.")
    folder_value = str(value.get("folder") or "").strip()
    route_value = str(value.get("route") or "").strip()
    return RssFeedSettings(
        url=url,
        name=str(value.get("name") or "").strip(),
        folder=_relative_folder(folder_value, f"{field_name}.folder") if folder_value else "",
        tags=[tag.strip() for tag in raw_tags],
        route=_relative_folder(route_value, f"{field_name}.route") if route_value else "",
        enabled=bool(value.get("enabled", True)),
    )


def _render_rss_feed(feed: RssFeedSettings | str) -> object:
    if isinstance(feed, str):
        return feed
    if not any((feed.name, feed.folder, feed.tags, feed.route)) and feed.enabled:
        return feed.url
    return {
        "url": feed.url,
        **({"name": feed.name} if feed.name else {}),
        **({"folder": feed.folder} if feed.folder else {}),
        **({"tags": feed.tags} if feed.tags else {}),
        **({"route": feed.route} if feed.route else {}),
        **({"enabled": False} if not feed.enabled else {}),
    }


_KNOWN_FAILURE_KINDS = ("dns", "timeout")


@dataclass(frozen=True)
class FetchRetrySettings:
    retry_base_minutes: int
    retry_max_days: int
    terminal_http_statuses: tuple[int, ...]
    terminal_failure_kinds: tuple[str, ...]
    terminal_kind_failures: int
    timeout_seconds: int
    browser_timeout_seconds: int


def fetch_retry_settings(config: VaultConfig) -> FetchRetrySettings:
    defaults = VaultConfig().fetch

    base_minutes = config.fetch.get("retry_base_minutes", defaults["retry_base_minutes"])
    if isinstance(base_minutes, bool) or not isinstance(base_minutes, int) or base_minutes < 1:
        raise ValueError("fetch.retry_base_minutes must be an integer >= 1.")

    max_days = config.fetch.get("retry_max_days", defaults["retry_max_days"])
    if isinstance(max_days, bool) or not isinstance(max_days, int) or max_days < 1:
        raise ValueError("fetch.retry_max_days must be an integer >= 1.")

    statuses = config.fetch.get("terminal_http_statuses", defaults["terminal_http_statuses"])
    if not isinstance(statuses, list) or not all(
        isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599
        for status in statuses
    ):
        raise ValueError("fetch.terminal_http_statuses must be a list of HTTP status codes (100-599).")
    deduplicated_statuses: list[int] = []
    for status in statuses:
        if status not in deduplicated_statuses:
            deduplicated_statuses.append(status)

    kinds = config.fetch.get("terminal_failure_kinds", defaults["terminal_failure_kinds"])
    if not isinstance(kinds, list) or not all(kind in _KNOWN_FAILURE_KINDS for kind in kinds):
        raise ValueError(f"fetch.terminal_failure_kinds must be a list containing only {_KNOWN_FAILURE_KINDS}.")
    deduplicated_kinds: list[str] = []
    for kind in kinds:
        if kind not in deduplicated_kinds:
            deduplicated_kinds.append(kind)

    kind_failures = config.fetch.get("terminal_kind_failures", defaults["terminal_kind_failures"])
    if isinstance(kind_failures, bool) or not isinstance(kind_failures, int) or kind_failures < 1:
        raise ValueError("fetch.terminal_kind_failures must be an integer >= 1.")

    timeout_seconds = config.fetch.get("timeout_seconds", defaults["timeout_seconds"])
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or timeout_seconds < 1:
        raise ValueError("fetch.timeout_seconds must be an integer >= 1.")

    browser_timeout_seconds = config.fetch.get("browser_timeout_seconds", defaults["browser_timeout_seconds"])
    if (
        isinstance(browser_timeout_seconds, bool)
        or not isinstance(browser_timeout_seconds, int)
        or browser_timeout_seconds < 1
    ):
        raise ValueError("fetch.browser_timeout_seconds must be an integer >= 1.")

    return FetchRetrySettings(
        retry_base_minutes=base_minutes,
        retry_max_days=max_days,
        terminal_http_statuses=tuple(deduplicated_statuses),
        terminal_failure_kinds=tuple(deduplicated_kinds),
        terminal_kind_failures=kind_failures,
        timeout_seconds=timeout_seconds,
        browser_timeout_seconds=browser_timeout_seconds,
    )


def normalized_rss_feeds(settings: ProviderSettings) -> list[RssFeedSettings]:
    """Normalize programmatically-created legacy string feed settings."""
    return [
        feed if isinstance(feed, RssFeedSettings) else _parse_rss_feed(feed, index=index)
        for index, feed in enumerate(settings.feeds)
    ]
