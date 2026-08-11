import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from feedian.config import Config, load_config


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
