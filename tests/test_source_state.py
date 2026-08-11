import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from feedian.source_state import load_raindrop_collection_count, save_raindrop_collection_count


class SourceStateTests(unittest.TestCase):
    def test_collection_count_round_trips_without_storing_the_token(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "source-state.json"

            save_raindrop_collection_count("secret-token", 0, True, 3163, state_path=state_path)

            self.assertEqual(
                load_raindrop_collection_count("secret-token", 0, True, state_path=state_path),
                3163,
            )
            self.assertNotIn("secret-token", state_path.read_text(encoding="utf-8"))

    def test_collection_counts_are_scoped_by_collection_and_nested_setting(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "source-state.json"
            save_raindrop_collection_count("token", 0, True, 100, state_path=state_path)
            save_raindrop_collection_count("token", 5, False, 20, state_path=state_path)

            self.assertEqual(load_raindrop_collection_count("token", 0, True, state_path=state_path), 100)
            self.assertEqual(load_raindrop_collection_count("token", 5, False, state_path=state_path), 20)
            self.assertIsNone(load_raindrop_collection_count("token", 5, True, state_path=state_path))

            data = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(len(data["raindrop_collection_counts"]), 2)


if __name__ == "__main__":
    unittest.main()
