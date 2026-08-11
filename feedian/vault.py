from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


VAULT_CONFIG_RELATIVE_PATH = Path(".feedian") / "config.json"
VAULT_DATABASE_NAME = "feedian.sqlite3"


@dataclass
class ProviderSettings:
    folder: str
    enabled: bool = True
    collection_id: int | None = None
    poll_hours: int | None = None
    feeds: list[str] = field(default_factory=list)


@dataclass
class VaultConfig:
    format_version: int = 1
    raw_folder: str = "raw"
    source_folder: str = "source"
    review_folder: str = "review"
    providers: dict[str, ProviderSettings] = field(
        default_factory=lambda: {
            "raindrop": ProviderSettings(folder="Raindrop", poll_hours=168),
            "hatena": ProviderSettings(folder="Hatena", poll_hours=168),
            "rss": ProviderSettings(folder="RSS", enabled=False, poll_hours=6),
        }
    )
    fetch: dict[str, Any] = field(
        default_factory=lambda: {
            "html_max_bytes": 10 * 1024 * 1024,
            "document_max_bytes": 100 * 1024 * 1024,
            "refresh_days": 30,
            "allow_private_hosts": [],
        }
    )

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
    raw_dir: Path
    source_dir: Path
    review_dir: Path
    assets_dir: Path
    logs_dir: Path


def vault_paths(root: str | Path) -> VaultPaths:
    resolved_root = Path(root).expanduser().resolve()
    state_dir = resolved_root / ".feedian"
    return VaultPaths(
        root=resolved_root,
        config_path=resolved_root / VAULT_CONFIG_RELATIVE_PATH,
        state_dir=state_dir,
        database_path=state_dir / VAULT_DATABASE_NAME,
        raw_dir=resolved_root / "raw",
        source_dir=resolved_root / "source",
        review_dir=resolved_root / "review",
        assets_dir=resolved_root / "raw" / "assets",
        logs_dir=state_dir / "logs",
    )


def user_settings_path() -> Path:
    if os.name == "nt":
        root = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return root / "Feedian" / "settings.json"
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return config_home / "feedian" / "settings.json"


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
    allowed = {"format_version", "raw_folder", "source_folder", "review_folder", "providers", "fetch"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"Unknown vault config field(s): {', '.join(unknown)}")
    providers = _parse_providers(raw.get("providers"))
    fetch = VaultConfig().fetch
    fetch.update(dict(raw.get("fetch") or {}))
    return VaultConfig(
        format_version=int(raw.get("format_version", 1)),
        raw_folder=_relative_folder(raw.get("raw_folder", "raw"), "raw_folder"),
        source_folder=_relative_folder(raw.get("source_folder", "source"), "source_folder"),
        review_folder=_relative_folder(raw.get("review_folder", "review"), "review_folder"),
        providers=providers,
        fetch=fetch,
    )


def render_vault_config(config: VaultConfig) -> str:
    providers = {
        name: {
            "folder": settings.folder,
            "enabled": settings.enabled,
            **({"collection_id": settings.collection_id} if settings.collection_id is not None else {}),
            **({"poll_hours": settings.poll_hours} if settings.poll_hours is not None else {}),
            **({"feeds": settings.feeds} if settings.feeds else {}),
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
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _parse_providers(raw: object) -> dict[str, ProviderSettings]:
    if raw is None:
        return VaultConfig().providers
    if not isinstance(raw, dict):
        raise ValueError("providers must be a JSON object.")
    providers: dict[str, ProviderSettings] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not isinstance(value, dict):
            raise ValueError("Each provider must have an object configuration.")
        allowed = {"folder", "enabled", "collection_id", "poll_hours", "feeds"}
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ValueError(f"Unknown provider field(s) for {name}: {', '.join(unknown)}")
        folder = _relative_folder(value.get("folder", name.title()), f"providers.{name}.folder")
        collection_id = value.get("collection_id")
        poll_hours = value.get("poll_hours")
        raw_feeds = value.get("feeds", [])
        if not isinstance(raw_feeds, list) or not all(isinstance(feed, str) and feed.strip() for feed in raw_feeds):
            raise ValueError(f"providers.{name}.feeds must be an array of non-empty URLs.")
        providers[name] = ProviderSettings(
            folder=folder,
            enabled=bool(value.get("enabled", True)),
            collection_id=int(collection_id) if collection_id is not None else None,
            poll_hours=max(1, int(poll_hours)) if poll_hours is not None else None,
            feeds=[feed.strip() for feed in raw_feeds],
        )
    return providers


def _relative_folder(value: object, field_name: str) -> str:
    text = str(value)
    path = Path(text)
    if not text or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{field_name} must be a non-empty relative path.")
    return path.as_posix()
