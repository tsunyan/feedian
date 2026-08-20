import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from feedian.config import Config, fetch_policy_from_config, load_config
from feedian.vault import VaultConfig


class ConfigTests(unittest.TestCase):
    def test_unknown_config_field_is_rejected(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            path.write_text(json.dumps({"vault_path": temp_dir, "max_artcle_chars": 10000}), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "max_artcle_chars"):
                load_config(path)

    def test_retry_settings_are_bounded(self) -> None:
        config = Config(vault_path="vault", max_retries=99, retry_base_seconds=0)

        self.assertEqual(config.max_retries, 5)
        self.assertEqual(config.retry_base_seconds, 0.1)

    def test_sync_request_interval_defaults_to_half_a_second(self) -> None:
        config = Config(vault_path="vault", sync_request_interval_seconds=-1)

        self.assertEqual(config.sync_request_interval_seconds, 0.0)
        self.assertEqual(Config(vault_path="vault").sync_request_interval_seconds, 0.5)


class FetchPolicyFromConfigTests(unittest.TestCase):
    """Spec 20260820-fetch-config-integrity-hardening, 改訂2: the legacy
    `--source hatena`-style CLI keeps Config.allow_private_urls as an
    all-or-nothing flag rather than growing a per-host allow-list."""

    def test_allow_private_urls_false_allows_no_private_host(self) -> None:
        config = Config(vault_path="vault", allow_private_urls=False)

        policy = fetch_policy_from_config(config)

        self.assertNotIn("localhost", policy.network.allowed_private_hosts)
        self.assertNotIn("anything.internal", policy.network.allowed_private_hosts)

    def test_allow_private_urls_true_allows_every_hostname(self) -> None:
        config = Config(vault_path="vault", allow_private_urls=True)

        policy = fetch_policy_from_config(config)

        self.assertIn("localhost", policy.network.allowed_private_hosts)
        self.assertIn("anything.internal", policy.network.allowed_private_hosts)

    def test_allow_private_urls_must_be_a_json_boolean(self) -> None:
        """bool("false") is True, so a coerced string would turn this flag's
        only guard into "allow every private address"."""
        for invalid in ("false", "true", 0, 1, None):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "allow_private_urls must be true or false"):
                    Config(vault_path="vault", allow_private_urls=invalid)

    def test_size_and_browser_timeout_defaults_match_vault_config(self) -> None:
        config = Config(vault_path="vault")
        vault_defaults = VaultConfig().fetch

        policy = fetch_policy_from_config(config)

        self.assertEqual(policy.html_max_bytes, vault_defaults["html_max_bytes"])
        self.assertEqual(policy.document_max_bytes, vault_defaults["document_max_bytes"])
        self.assertEqual(policy.browser_timeout_seconds, vault_defaults["browser_timeout_seconds"])

    def test_timeout_seconds_comes_from_config_request_timeout(self) -> None:
        config = Config(vault_path="vault", request_timeout_seconds=17)

        policy = fetch_policy_from_config(config)

        self.assertEqual(policy.timeout_seconds, 17)
