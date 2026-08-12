from __future__ import annotations

import json

import pytest

from feedian.vault import find_vault_root, initialize_vault, load_vault_config, save_default_vault, user_settings_path


def test_initialize_vault_creates_portable_config(tmp_path) -> None:
    root = tmp_path / "vault"
    root.mkdir()

    paths = initialize_vault(root)
    config = load_vault_config(root)

    assert paths.config_path == root / ".feedian" / "config.json"
    ignored = (root / ".feedian" / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert {"staging/", "tmp/", "scheduled-run.cmd"}.issubset(ignored)
    assert config.raw_folder == "raw"
    assert config.provider_output_folder("hatena").as_posix() == "raw/Hatena"
    assert "vault_path" not in json.loads(paths.config_path.read_text(encoding="utf-8"))


def test_find_vault_walks_up_from_current_directory(tmp_path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    initialize_vault(root)
    child = root / "raw" / "Hatena"
    child.mkdir(parents=True)

    assert find_vault_root(cwd=child) == root


def test_find_vault_requires_existing_explicit_config(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        find_vault_root(explicit=str(tmp_path / "missing"))


def test_save_default_vault_uses_user_settings(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    root = tmp_path / "vault"
    root.mkdir()

    path = save_default_vault(root)

    assert path == user_settings_path()
    assert json.loads(path.read_text(encoding="utf-8"))["default_vault"] == str(root.resolve())


def test_load_vault_config_accepts_rss_feeds(tmp_path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    initialize_vault(root)
    config_path = root / ".feedian" / "config.json"
    config_path.write_text(
        '{"providers":{"rss":{"folder":"RSS","enabled":true,"poll_hours":6,"feeds":["https://example.test/feed.xml"]}}}',
        encoding="utf-8",
    )
    config = load_vault_config(root)
    assert config.providers["rss"].feeds[0].url == "https://example.test/feed.xml"
    assert config.providers["rss"].layout == "feed/year/month"


def test_load_vault_config_accepts_rss_feed_routing(tmp_path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    initialize_vault(root)
    config_path = root / ".feedian" / "config.json"
    config_path.write_text(
        """{
          "providers": {
            "rss": {
              "folder": "RSS",
              "enabled": true,
              "layout": "route/feed/year/month",
              "category_routes": {"AI": "technology/ai"},
              "feeds": [{
                "url": "https://example.test/feed.xml",
                "name": "Example",
                "folder": "Example Feed",
                "tags": ["news"],
                "route": "reading"
              }]
            }
          }
        }""",
        encoding="utf-8",
    )

    config = load_vault_config(root)
    settings = config.providers["rss"]

    assert settings.layout == "route/feed/year/month"
    assert settings.category_routes == {"AI": "technology/ai"}
    assert settings.feeds[0].folder == "Example Feed"
    assert settings.feeds[0].tags == ["news"]
    assert settings.feeds[0].route == "reading"
