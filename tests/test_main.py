import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from raindian.__main__ import existing_note_for_item, parse_args, process_bookmarks, write_note_atomically
from raindian.config import Config


class MainTests(unittest.TestCase):
    def test_write_note_atomically_replaces_existing_note(self) -> None:
        with TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "note.md"
            target.write_text("old", encoding="utf-8")

            write_note_atomically(target, "new")

            self.assertEqual(target.read_text(encoding="utf-8"), "new")
            self.assertEqual(list(Path(temp_dir).glob("*.tmp")), [])

    def test_existing_note_for_item_uses_raindrop_id_suffix(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir)
            existing = path / "Old title - 123.md"
            existing.write_text("", encoding="utf-8")
            self.assertEqual(existing_note_for_item(path, {"_id": 123}), existing)

    def test_summary_failure_does_not_stop_later_bookmarks(self) -> None:
        items = [
            {"_id": 1, "title": "First", "link": "https://example.com/first", "collection": {}},
            {"_id": 2, "title": "Second", "link": "https://example.com/second", "collection": {}},
        ]
        summary = {
            "note_title": "Second",
            "summary": "ok",
            "key_points": [],
            "tags": [],
            "content_type": "link",
        }

        with TemporaryDirectory() as temp_dir:
            config = Config(vault_path=temp_dir, sleep_seconds=0)
            args = parse_args(["--skip-page-fetch"])
            with (
                patch.dict(os.environ, {"RAINDROP_TOKEN": "token", "OPENAI_API_KEY": "key"}),
                patch("raindian.__main__.RaindropClient.iter_raindrops", return_value=iter(items)),
                patch("raindian.__main__.summarize_bookmark", side_effect=[RuntimeError("rate limited"), summary]),
            ):
                result = process_bookmarks(config, args)

            destination = Path(temp_dir) / "Raindrop"
            self.assertEqual(result, 1)
            self.assertFalse((destination / "First - 1.md").exists())
            self.assertTrue((destination / "Second - 2.md").exists())

    def test_dry_run_does_not_fetch_pages_or_call_openai(self) -> None:
        items = [{"_id": 1, "title": "First", "link": "https://example.com/first", "collection": {}}]

        with TemporaryDirectory() as temp_dir:
            config = Config(vault_path=temp_dir, sleep_seconds=0)
            args = parse_args(["--dry-run"])
            with (
                patch.dict(os.environ, {"RAINDROP_TOKEN": "token"}, clear=True),
                patch("raindian.__main__.RaindropClient.iter_raindrops", return_value=iter(items)),
                patch("raindian.__main__.fetch_page_text") as fetch_page,
                patch("raindian.__main__.summarize_bookmark") as summarize,
            ):
                result = process_bookmarks(config, args)

        self.assertEqual(result, 0)
        fetch_page.assert_not_called()
        summarize.assert_not_called()
        self.assertFalse((Path(temp_dir) / "Raindrop").exists())


if __name__ == "__main__":
    unittest.main()
