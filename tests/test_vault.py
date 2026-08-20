from __future__ import annotations

import json
import unittest

import pytest

from feedian.vault import (
    FetchPolicy,
    FetchRetrySettings,
    NetworkPolicy,
    VaultConfig,
    fetch_policy,
    fetch_retry_settings,
    find_vault_root,
    initialize_vault,
    load_vault_config,
    migrate_vault_config,
    render_vault_config,
    save_default_vault,
    user_settings_path,
)


def test_initialize_vault_creates_portable_config(tmp_path) -> None:
    root = tmp_path / "vault"
    root.mkdir()

    paths = initialize_vault(root)
    config = load_vault_config(root)

    assert paths.config_path == root / ".feedian" / "config.json"
    ignored = (root / ".feedian" / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert {"staging/", "tmp/", "scheduled-run.cmd"}.issubset(ignored)
    assert config.raw_folder == "raw"
    assert config.format_version == 2
    assert config.llm.backend == "openai-responses"
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
        '{"format_version":2,"providers":{"rss":{"folder":"RSS","enabled":true,"poll_hours":6,"feeds":["https://example.test/feed.xml"]}}}',
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
          "format_version": 2,
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


def test_v1_config_requires_explicit_migration(tmp_path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    initialize_vault(root)
    config_path = root / ".feedian" / "config.json"
    config_path.write_text('{"format_version":1,"source_folder":"notes"}', encoding="utf-8")

    with pytest.raises(RuntimeError, match="migration is required"):
        load_vault_config(root)

    assert migrate_vault_config(root) is True
    config = load_vault_config(root)
    assert config.format_version == 2
    assert config.source_folder == "notes"
    assert config.llm.backend == "openai-responses"
    assert migrate_vault_config(root) is False


def test_enabled_fallback_requires_both_a_backend_and_a_model(tmp_path) -> None:
    """Feedian never picks the destination itself, so the config must name it."""

    root = tmp_path / "vault"
    root.mkdir()
    initialize_vault(root)
    config_path = root / ".feedian" / "config.json"
    config_path.write_text(
        '{"format_version":2,"llm":{"backend":"codex-local","model":"gpt-test",'
        '"fallback":{"enabled":true,"backend":"openai-responses"}}}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="requires both backend and model"):
        load_vault_config(root)

    config_path.write_text(
        '{"format_version":2,"llm":{"backend":"codex-local","model":"gpt-test",'
        '"fallback":{"enabled":true,"backend":"openai-responses","model":"gpt-5.6-terra"}}}',
        encoding="utf-8",
    )
    config = load_vault_config(root)

    assert config.llm.fallback.enabled
    assert config.llm.fallback.backend == "openai-responses"
    assert config.llm.fallback.model == "gpt-5.6-terra"


class FetchRetrySettingsTests(unittest.TestCase):
    def test_defaults_when_keys_are_absent(self) -> None:
        settings = fetch_retry_settings(VaultConfig())

        self.assertEqual(
            settings,
            FetchRetrySettings(
                retry_base_minutes=30,
                retry_max_days=30,
                terminal_http_statuses=(404, 410),
                terminal_failure_kinds=("dns", "timeout"),
                terminal_kind_failures=3,
                timeout_seconds=5,
                browser_timeout_seconds=30,
            ),
        )

    def test_explicit_values_come_through_as_a_dataclass(self) -> None:
        config = VaultConfig()
        config.fetch["retry_base_minutes"] = 5
        config.fetch["retry_max_days"] = 7
        config.fetch["terminal_http_statuses"] = [404, 451]
        config.fetch["terminal_failure_kinds"] = ["dns"]
        config.fetch["terminal_kind_failures"] = 4
        config.fetch["timeout_seconds"] = 8
        config.fetch["browser_timeout_seconds"] = 45

        settings = fetch_retry_settings(config)

        self.assertEqual(settings.retry_base_minutes, 5)
        self.assertEqual(settings.retry_max_days, 7)
        self.assertEqual(settings.terminal_http_statuses, (404, 451))
        self.assertIsInstance(settings.terminal_http_statuses, tuple)
        self.assertEqual(settings.terminal_failure_kinds, ("dns",))
        self.assertIsInstance(settings.terminal_failure_kinds, tuple)
        self.assertEqual(settings.terminal_kind_failures, 4)
        self.assertEqual(settings.timeout_seconds, 8)
        self.assertEqual(settings.browser_timeout_seconds, 45)

    def test_empty_terminal_http_statuses_disables_the_mechanism(self) -> None:
        config = VaultConfig()
        config.fetch["terminal_http_statuses"] = []

        settings = fetch_retry_settings(config)

        self.assertEqual(settings.terminal_http_statuses, ())

    def test_duplicate_terminal_http_statuses_are_deduplicated_in_order(self) -> None:
        config = VaultConfig()
        config.fetch["terminal_http_statuses"] = [410, 404, 410, 404]

        settings = fetch_retry_settings(config)

        self.assertEqual(settings.terminal_http_statuses, (410, 404))

    def test_retry_base_minutes_rejects_invalid_values(self) -> None:
        for invalid in (1.5, True, "2", 0, -1):
            with self.subTest(invalid=invalid):
                config = VaultConfig()
                config.fetch["retry_base_minutes"] = invalid

                with self.assertRaisesRegex(ValueError, "retry_base_minutes"):
                    fetch_retry_settings(config)

    def test_retry_max_days_rejects_zero(self) -> None:
        config = VaultConfig()
        config.fetch["retry_max_days"] = 0

        with self.assertRaisesRegex(ValueError, "retry_max_days"):
            fetch_retry_settings(config)

    def test_terminal_http_statuses_rejects_invalid_values(self) -> None:
        for invalid in (404, [True], [99], [600], ["404"]):
            with self.subTest(invalid=invalid):
                config = VaultConfig()
                config.fetch["terminal_http_statuses"] = invalid

                with self.assertRaisesRegex(ValueError, "terminal_http_statuses"):
                    fetch_retry_settings(config)

    def test_terminal_failure_kinds_accepts_an_empty_list(self) -> None:
        config = VaultConfig()
        config.fetch["terminal_failure_kinds"] = []

        settings = fetch_retry_settings(config)

        self.assertEqual(settings.terminal_failure_kinds, ())

    def test_terminal_failure_kinds_deduplicates_in_order(self) -> None:
        config = VaultConfig()
        config.fetch["terminal_failure_kinds"] = ["timeout", "dns", "timeout", "dns"]

        settings = fetch_retry_settings(config)

        self.assertEqual(settings.terminal_failure_kinds, ("timeout", "dns"))

    def test_terminal_failure_kinds_rejects_unknown_values(self) -> None:
        for invalid in (["dns", "ssl"], ["unknown"], "dns", [1]):
            with self.subTest(invalid=invalid):
                config = VaultConfig()
                config.fetch["terminal_failure_kinds"] = invalid

                with self.assertRaisesRegex(ValueError, "terminal_failure_kinds"):
                    fetch_retry_settings(config)

    def test_terminal_kind_failures_rejects_invalid_values(self) -> None:
        for invalid in (1.5, True, "2", 0, -1):
            with self.subTest(invalid=invalid):
                config = VaultConfig()
                config.fetch["terminal_kind_failures"] = invalid

                with self.assertRaisesRegex(ValueError, "terminal_kind_failures"):
                    fetch_retry_settings(config)

    def test_timeout_seconds_rejects_invalid_values(self) -> None:
        for invalid in (1.5, True, "2", 0, -1):
            with self.subTest(invalid=invalid):
                config = VaultConfig()
                config.fetch["timeout_seconds"] = invalid

                with self.assertRaisesRegex(ValueError, "timeout_seconds"):
                    fetch_retry_settings(config)

    def test_browser_timeout_seconds_rejects_invalid_values(self) -> None:
        for invalid in (1.5, True, "2", 0, -1):
            with self.subTest(invalid=invalid):
                config = VaultConfig()
                config.fetch["browser_timeout_seconds"] = invalid

                with self.assertRaisesRegex(ValueError, "browser_timeout_seconds"):
                    fetch_retry_settings(config)


class FetchPolicyTests(unittest.TestCase):
    def test_defaults_when_keys_are_absent(self) -> None:
        policy = fetch_policy(VaultConfig())

        self.assertEqual(
            policy,
            FetchPolicy(
                network=NetworkPolicy(allowed_private_hosts=frozenset()),
                html_max_bytes=10 * 1024 * 1024,
                document_max_bytes=100 * 1024 * 1024,
                timeout_seconds=5,
                browser_timeout_seconds=30,
            ),
        )

    def test_explicit_values_come_through_as_a_dataclass(self) -> None:
        config = VaultConfig()
        config.fetch["html_max_bytes"] = 1024
        config.fetch["document_max_bytes"] = 2048
        config.fetch["timeout_seconds"] = 8
        config.fetch["browser_timeout_seconds"] = 45
        config.fetch["allow_private_hosts"] = ["Internal.Example.Test"]

        policy = fetch_policy(config)

        self.assertEqual(policy.html_max_bytes, 1024)
        self.assertEqual(policy.document_max_bytes, 2048)
        self.assertEqual(policy.timeout_seconds, 8)
        self.assertEqual(policy.browser_timeout_seconds, 45)
        self.assertEqual(policy.network.allowed_private_hosts, frozenset({"internal.example.test"}))

    def test_html_max_bytes_rejects_invalid_values(self) -> None:
        for invalid in (1.5, True, "2", 0, -1):
            with self.subTest(invalid=invalid):
                config = VaultConfig()
                config.fetch["html_max_bytes"] = invalid

                with self.assertRaisesRegex(ValueError, "html_max_bytes"):
                    fetch_policy(config)

    def test_document_max_bytes_rejects_invalid_values(self) -> None:
        for invalid in (1.5, True, "2", 0, -1):
            with self.subTest(invalid=invalid):
                config = VaultConfig()
                config.fetch["document_max_bytes"] = invalid

                with self.assertRaisesRegex(ValueError, "document_max_bytes"):
                    fetch_policy(config)

    def test_allow_private_hosts_with_one_host_only_skips_that_host(self) -> None:
        config = VaultConfig()
        config.fetch["allow_private_hosts"] = ["internal.example.test"]

        policy = fetch_policy(config)

        self.assertIn("internal.example.test", policy.network.allowed_private_hosts)
        self.assertNotIn("other-internal.example.test", policy.network.allowed_private_hosts)
        self.assertNotIn("localhost", policy.network.allowed_private_hosts)

    def test_allow_private_hosts_trims_lowercases_and_deduplicates(self) -> None:
        config = VaultConfig()
        config.fetch["allow_private_hosts"] = [" Internal.Example.Test ", "internal.example.test"]

        policy = fetch_policy(config)

        self.assertEqual(policy.network.allowed_private_hosts, frozenset({"internal.example.test"}))

    def test_allow_private_hosts_rejects_non_array_input(self) -> None:
        for invalid in ("internal.example.test", 1, {"a": 1}, None):
            with self.subTest(invalid=invalid):
                config = VaultConfig()
                config.fetch["allow_private_hosts"] = invalid

                with self.assertRaisesRegex(ValueError, "allow_private_hosts"):
                    fetch_policy(config)

    def test_allow_private_hosts_rejects_empty_entries(self) -> None:
        config = VaultConfig()
        config.fetch["allow_private_hosts"] = ["   "]

        with self.assertRaisesRegex(ValueError, "allow_private_hosts"):
            fetch_policy(config)

    def test_allow_private_hosts_rejects_non_string_entries(self) -> None:
        config = VaultConfig()
        config.fetch["allow_private_hosts"] = [123]

        with self.assertRaisesRegex(ValueError, "allow_private_hosts"):
            fetch_policy(config)


def _write_config(root, payload: dict) -> None:
    (root / ".feedian").mkdir(parents=True, exist_ok=True)
    (root / ".feedian" / "config.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _base_config(**overrides) -> dict:
    payload = {
        "format_version": 2,
        "raw_folder": "raw",
        "source_folder": "source",
        "review_folder": "review",
        "providers": {"raindrop": {"folder": "Raindrop", "enabled": True}},
        "fetch": {},
        "llm": {"backend": "openai-responses", "model": "gpt-5.6-terra"},
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize("key", ["workers", "comment_workers", "quick_stop_after_known_pages"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5, "8"])
def test_worker_settings_are_rejected_at_config_load(tmp_path, key, value) -> None:
    """Spec 20260819-sync-ingest-throughput: one rule, applied when the file is read."""
    _write_config(tmp_path, _base_config(fetch={key: value}))

    with pytest.raises(ValueError):
        load_vault_config(tmp_path)


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "8"])
def test_llm_workers_is_rejected_at_config_load(tmp_path, value) -> None:
    _write_config(tmp_path, _base_config(llm={"backend": "openai-responses", "model": "m", "workers": value}))

    with pytest.raises(ValueError):
        load_vault_config(tmp_path)


def test_llm_workers_round_trips_without_a_format_version_bump(tmp_path) -> None:
    _write_config(tmp_path, _base_config(llm={"backend": "openai-responses", "model": "m", "workers": 3}))

    config = load_vault_config(tmp_path)
    rendered = json.loads(render_vault_config(config))

    assert config.llm.workers == 3
    assert rendered["llm"]["workers"] == 3, "always emitted, so the setting is visible in the file"
    assert rendered["format_version"] == 2, "an optional key with a default needs no migration"


def test_a_config_written_before_llm_workers_existed_takes_the_default(tmp_path) -> None:
    _write_config(tmp_path, _base_config())

    config = load_vault_config(tmp_path)

    assert config.llm.workers == 8
    assert config.fetch["workers"] == 8


def test_unknown_llm_field_is_still_rejected(tmp_path) -> None:
    _write_config(tmp_path, _base_config(llm={"backend": "openai-responses", "model": "m", "threads": 4}))

    with pytest.raises(ValueError, match="Unknown llm field"):
        load_vault_config(tmp_path)


@pytest.mark.parametrize("value", ["false", "true", 0, 1])
def test_provider_enabled_is_rejected_when_not_a_json_boolean(tmp_path, value) -> None:
    """Spec 20260820-fetch-config-integrity-hardening: bool("false") == True, so this must fail-fast."""
    _write_config(tmp_path, _base_config(providers={"raindrop": {"folder": "Raindrop", "enabled": value}}))

    with pytest.raises(ValueError):
        load_vault_config(tmp_path)


def test_provider_enabled_false_disables_the_provider(tmp_path) -> None:
    _write_config(tmp_path, _base_config(providers={"raindrop": {"folder": "Raindrop", "enabled": False}}))

    config = load_vault_config(tmp_path)

    assert config.providers["raindrop"].enabled is False


@pytest.mark.parametrize("value", ["false", "true", 0, 1])
def test_rss_feed_enabled_is_rejected_when_not_a_json_boolean(tmp_path, value) -> None:
    _write_config(
        tmp_path,
        _base_config(
            providers={
                "rss": {
                    "folder": "RSS",
                    "enabled": True,
                    "feeds": [{"url": "https://example.test/feed.xml", "enabled": value}],
                }
            }
        ),
    )

    with pytest.raises(ValueError):
        load_vault_config(tmp_path)


def test_rss_feed_enabled_false_round_trips(tmp_path) -> None:
    _write_config(
        tmp_path,
        _base_config(
            providers={
                "rss": {
                    "folder": "RSS",
                    "enabled": True,
                    "feeds": [{"url": "https://example.test/feed.xml", "enabled": False}],
                }
            }
        ),
    )

    config = load_vault_config(tmp_path)

    assert config.providers["rss"].feeds[0].enabled is False
