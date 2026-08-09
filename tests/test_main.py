import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from raindian.__main__ import (
    estimate_bookmarks,
    existing_note_for_item,
    list_collections,
    main,
    parse_args,
    process_bookmarks,
    write_note_atomically,
)
from raindian.config import Config
from raindian.extract import PageFetchResult


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

    def test_list_collections_accepts_null_parent(self) -> None:
        client = Mock()
        client.get_root_collections.return_value = [{"_id": 1, "title": "Root", "count": 2, "parent": None}]
        client.get_child_collections.return_value = [
            {"_id": 1, "title": "Root", "count": 2, "parent": None},
            {"_id": 2, "title": "Child", "count": 1, "parent": {"$id": 1}},
        ]

        output = StringIO()
        with redirect_stdout(output):
            list_collections(client)

        self.assertEqual(output.getvalue().splitlines(), ["1\tRoot\tcount=2", "2\tChild\tcount=1 parent=1"])

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

    def test_estimate_uses_raindrop_and_page_fetch_without_openai_or_writes(self) -> None:
        items = [{"_id": 1, "title": "First", "link": "https://example.com/first", "collection": {}}]
        page = PageFetchResult(url=items[0]["link"], text="Example page text.", title="Example", error=None)

        with TemporaryDirectory() as temp_dir:
            config = Config(vault_path=temp_dir, sleep_seconds=0)
            args = parse_args(["--estimate", "--estimate-sample-size", "1"])
            output = StringIO()
            with (
                patch.dict(os.environ, {"RAINDROP_TOKEN": "token"}, clear=True),
                patch("raindian.__main__.RaindropClient.iter_raindrops", return_value=iter(items)),
                patch("raindian.__main__.fetch_page_text", return_value=page) as fetch_page,
                patch("raindian.__main__.summarize_bookmark") as summarize,
                redirect_stdout(output),
            ):
                result = estimate_bookmarks(config, args)

        self.assertEqual(result, 0)
        fetch_page.assert_called_once()
        summarize.assert_not_called()
        self.assertFalse((Path(temp_dir) / "Raindrop").exists())
        self.assertIn("GPT-5.6 Sol", output.getvalue())
        self.assertIn("GPT-5.6 Luna [selected]", output.getvalue())
        self.assertIn("phase=collecting-bookmarks", output.getvalue())
        self.assertIn("phase=sampling", output.getvalue())
        self.assertIn("phase=fetching-pages", output.getvalue())
        self.assertIn("phase=calculating-costs", output.getvalue())

    def test_estimate_counts_the_shared_developer_instructions(self) -> None:
        items = [{"_id": 1, "title": "First", "link": "https://example.com/first", "collection": {}}]
        page = PageFetchResult(url=items[0]["link"], text="Example page text.", title="Example", error=None)

        with TemporaryDirectory() as temp_dir:
            config = Config(vault_path=temp_dir, sleep_seconds=0)
            args = parse_args(["--estimate", "--estimate-sample-size", "1"])
            with (
                patch.dict(os.environ, {"RAINDROP_TOKEN": "token"}, clear=True),
                patch("raindian.__main__.RaindropClient.iter_raindrops", return_value=iter(items)),
                patch("raindian.__main__.fetch_page_text", return_value=page),
                patch("raindian.__main__.count_prompt_tokens", return_value=(10, None)) as count_tokens,
                redirect_stdout(StringIO()),
            ):
                estimate_bookmarks(config, args)

        self.assertIn("You summarize bookmarked web pages", count_tokens.call_args.args[0])

    def test_estimate_count_only_does_not_fetch_pages(self) -> None:
        items = [{"_id": 1, "title": "First", "link": "https://example.com/first", "collection": {}}]

        with TemporaryDirectory() as temp_dir:
            config = Config(vault_path=temp_dir, sleep_seconds=0)
            args = parse_args(["--estimate", "--estimate-sample-size", "0"])
            with (
                patch.dict(os.environ, {"RAINDROP_TOKEN": "token"}, clear=True),
                patch("raindian.__main__.RaindropClient.iter_raindrops", return_value=iter(items)),
                patch("raindian.__main__.fetch_page_text") as fetch_page,
            ):
                result = estimate_bookmarks(config, args)

        self.assertEqual(result, 0)
        fetch_page.assert_not_called()

    def test_estimate_respects_skip_page_fetch(self) -> None:
        items = [{"_id": 1, "title": "First", "link": "https://example.com/first", "collection": {}}]

        with TemporaryDirectory() as temp_dir:
            config = Config(vault_path=temp_dir, sleep_seconds=0)
            args = parse_args(["--estimate", "--estimate-sample-size", "1", "--skip-page-fetch"])
            with (
                patch.dict(os.environ, {"RAINDROP_TOKEN": "token"}, clear=True),
                patch("raindian.__main__.RaindropClient.iter_raindrops", return_value=iter(items)),
                patch("raindian.__main__.fetch_page_text") as fetch_page,
                redirect_stdout(StringIO()),
            ):
                result = estimate_bookmarks(config, args)

        self.assertEqual(result, 0)
        fetch_page.assert_not_called()

    def test_estimate_uses_metadata_when_every_page_fetch_fails(self) -> None:
        items = [{"_id": 1, "title": "First", "link": "https://example.com/first", "collection": {}}]
        failed_page = PageFetchResult(url=items[0]["link"], text="", title="", error="HTTP 503")

        with TemporaryDirectory() as temp_dir:
            config = Config(vault_path=temp_dir, sleep_seconds=0)
            args = parse_args(["--estimate", "--estimate-sample-size", "1"])
            output = StringIO()
            with (
                patch.dict(os.environ, {"RAINDROP_TOKEN": "token"}, clear=True),
                patch("raindian.__main__.RaindropClient.iter_raindrops", return_value=iter(items)),
                patch("raindian.__main__.fetch_page_text", return_value=failed_page),
                redirect_stdout(output),
            ):
                result = estimate_bookmarks(config, args)

        self.assertEqual(result, 0)
        self.assertIn("sampled=1", output.getvalue())
        self.assertIn("GPT-5.6 Sol", output.getvalue())
        self.assertIn("page fetch failure: 1 x HTTP 503", output.getvalue())

    def test_estimate_reports_empty_target_without_page_fetch(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config = Config(vault_path=temp_dir, sleep_seconds=0)
            args = parse_args(["--estimate"])
            output = StringIO()
            with (
                patch.dict(os.environ, {"RAINDROP_TOKEN": "token"}, clear=True),
                patch("raindian.__main__.RaindropClient.iter_raindrops", return_value=iter(())),
                patch("raindian.__main__.fetch_page_text") as fetch_page,
                redirect_stdout(output),
            ):
                result = estimate_bookmarks(config, args)

        self.assertEqual(result, 0)
        self.assertIn("target=0", output.getvalue())
        fetch_page.assert_not_called()

    def test_main_rejects_estimate_with_dry_run(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            config_path.write_text('{"vault_path": "C:/vault"}', encoding="utf-8")
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                result = main(["--config", str(config_path), "--estimate", "--dry-run"])

        self.assertEqual(result, 1)


if __name__ == "__main__":
    unittest.main()
