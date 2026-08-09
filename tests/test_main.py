import os
import json
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
    sync_raindrop_tags,
    sync_raindrop_summaries,
    usage_record_exists,
    write_note_atomically,
)
from raindian.config import Config
from raindian.estimate import MODEL_PRICES, PriceRefresh
from raindian.extract import PageFetchResult


class MainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.price_refresh = patch(
            "raindian.__main__.refresh_model_prices",
            return_value=PriceRefresh(prices=MODEL_PRICES, source="official", warning=None),
        )
        self.price_refresh.start()
        self.addCleanup(self.price_refresh.stop)

    def test_write_note_atomically_replaces_existing_note(self) -> None:
        with TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "note.md"
            target.write_text("old", encoding="utf-8")

            write_note_atomically(target, "new")

            self.assertEqual(target.read_text(encoding="utf-8"), "new")
            self.assertEqual(list(Path(temp_dir).glob("*.tmp")), [])

    def test_usage_record_exists_matches_only_its_transaction_id(self) -> None:
        with TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "Raindrop"
            destination.mkdir()
            (destination / ".raindian-usage.jsonl").write_text(
                '{"transaction_id":"txn-a"}\nnot-json\n',
                encoding="utf-8",
            )

            self.assertTrue(usage_record_exists(destination, "txn-a"))
            self.assertFalse(usage_record_exists(destination, "txn-b"))

    def test_existing_note_for_item_uses_raindrop_id_suffix(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir)
            existing = path / "Old title - 123.md"
            existing.write_text("", encoding="utf-8")
            self.assertEqual(existing_note_for_item(path, {"_id": 123}), existing)

    def test_llm_run_upgrades_an_existing_no_llm_note(self) -> None:
        item = {"_id": 123, "title": "First", "link": "https://example.com/first", "collection": {}}
        summary = {
            "note_title": "First",
            "summary": "LLM summary",
            "key_points": [],
            "tags": [],
            "content_type": "link",
        }

        with TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "Raindrop"
            destination.mkdir()
            existing = destination / "First - 123.md"
            existing.write_text('---\nsummary_generated_at: "old"\n---\n\nNo LLM summary\n', encoding="utf-8")
            config = Config(vault_path=temp_dir, sleep_seconds=0)
            args = parse_args(["--skip-page-fetch"])
            with (
                patch.dict(os.environ, {"RAINDROP_TOKEN": "token", "OPENAI_API_KEY": "key"}),
                patch("raindian.__main__.RaindropClient.iter_raindrops", return_value=iter([item])),
                patch("raindian.__main__.summarize_bookmark", return_value=summary),
            ):
                result = process_bookmarks(config, args)

            rendered = existing.read_text(encoding="utf-8")

        self.assertEqual(result, 0)
        self.assertIn("LLM summary", rendered)
        self.assertIn('summary_model: "gpt-5.6-luna"', rendered)

    def test_new_llm_note_uses_the_japanese_note_title_for_its_filename(self) -> None:
        item = {"_id": 123, "title": "Original foreign title", "link": "https://example.com/first", "collection": {}}
        summary = {
            "note_title": "日本語のタイトル",
            "summary": "LLM summary",
            "key_points": [],
            "tags": [],
            "content_type": "link",
        }

        with TemporaryDirectory() as temp_dir:
            config = Config(vault_path=temp_dir, sleep_seconds=0)
            args = parse_args(["--skip-page-fetch"])
            with (
                patch.dict(os.environ, {"RAINDROP_TOKEN": "token", "OPENAI_API_KEY": "key"}),
                patch("raindian.__main__.RaindropClient.iter_raindrops", return_value=iter([item])),
                patch("raindian.__main__.summarize_bookmark", return_value=summary),
            ):
                result = process_bookmarks(config, args)

            destination = Path(temp_dir) / "Raindrop"
            created = destination / "日本語のタイトル - 123.md"
            self.assertTrue(created.exists())

        self.assertEqual(result, 0)

    def test_rename_existing_moves_an_llm_note_without_calling_openai(self) -> None:
        item = {"_id": 123, "title": "Original foreign title", "link": "https://example.com/first", "collection": {}}

        with TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "Raindrop"
            destination.mkdir()
            old_path = destination / "Original foreign title - 123.md"
            old_path.write_text(
                '---\ntitle: "日本語のタイトル"\nsummary_model: "gpt-5.6-luna"\n---\n\nSummary',
                encoding="utf-8",
            )
            config = Config(vault_path=temp_dir, sleep_seconds=0)
            args = parse_args(["--rename-existing"])
            with (
                patch.dict(os.environ, {"RAINDROP_TOKEN": "token", "OPENAI_API_KEY": "key"}),
                patch("raindian.__main__.RaindropClient.iter_raindrops", return_value=iter([item])),
                patch("raindian.__main__.summarize_bookmark") as summarize,
            ):
                result = process_bookmarks(config, args)

            new_path = destination / "日本語のタイトル - 123.md"
            self.assertTrue(new_path.exists())
            self.assertFalse(old_path.exists())
            summarize.assert_not_called()

        self.assertEqual(result, 0)

    def test_rename_existing_moves_an_upgraded_no_llm_note_after_writing_the_summary(self) -> None:
        item = {"_id": 123, "title": "Original foreign title", "link": "https://example.com/first", "collection": {}}
        summary = {
            "note_title": "日本語のタイトル",
            "summary": "LLM summary",
            "key_points": [],
            "tags": [],
            "content_type": "link",
        }

        with TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "Raindrop"
            destination.mkdir()
            old_path = destination / "Original foreign title - 123.md"
            old_path.write_text('---\nsummary_generated_at: "old"\n---\n\nNo LLM summary\n', encoding="utf-8")
            config = Config(vault_path=temp_dir, sleep_seconds=0)
            args = parse_args(["--rename-existing", "--skip-page-fetch"])
            with (
                patch.dict(os.environ, {"RAINDROP_TOKEN": "token", "OPENAI_API_KEY": "key"}),
                patch("raindian.__main__.RaindropClient.iter_raindrops", return_value=iter([item])),
                patch("raindian.__main__.summarize_bookmark", return_value=summary),
            ):
                result = process_bookmarks(config, args)

            new_path = destination / "日本語のタイトル - 123.md"
            self.assertTrue(new_path.exists())
            self.assertFalse(old_path.exists())

        self.assertEqual(result, 0)

    def test_no_llm_does_not_overwrite_an_existing_llm_note(self) -> None:
        item = {"_id": 123, "title": "First", "link": "https://example.com/first", "collection": {}}

        with TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "Raindrop"
            destination.mkdir()
            existing = destination / "First - 123.md"
            original = '---\nsummary_model: "gpt-5.6-luna"\n---\n\nLLM summary\n'
            existing.write_text(original, encoding="utf-8")
            config = Config(vault_path=temp_dir, sleep_seconds=0)
            args = parse_args(["--no-llm", "--skip-page-fetch"])
            with (
                patch.dict(os.environ, {"RAINDROP_TOKEN": "token"}, clear=True),
                patch("raindian.__main__.RaindropClient.iter_raindrops", return_value=iter([item])),
            ):
                result = process_bookmarks(config, args)

            rendered = existing.read_text(encoding="utf-8")

        self.assertEqual(result, 0)
        self.assertEqual(rendered, original)

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

    def test_successful_llm_summary_records_usage_without_page_content(self) -> None:
        item = {"_id": 123, "title": "First", "link": "https://example.com/first", "collection": {}}
        summary = {
            "note_title": "First",
            "summary": "ok",
            "key_points": [],
            "tags": [],
            "content_type": "link",
            "_raindian_usage": {
                "input_tokens": 120,
                "cached_input_tokens": 20,
                "output_tokens": 34,
                "reasoning_tokens": 8,
                "total_tokens": 154,
            },
        }

        with TemporaryDirectory() as temp_dir:
            config = Config(vault_path=temp_dir, sleep_seconds=0)
            args = parse_args(["--skip-page-fetch"])
            with (
                patch.dict(os.environ, {"RAINDROP_TOKEN": "token", "OPENAI_API_KEY": "key"}),
                patch("raindian.__main__.RaindropClient.iter_raindrops", return_value=iter([item])),
                patch("raindian.__main__.summarize_bookmark", return_value=summary),
            ):
                result = process_bookmarks(config, args)

            usage_path = Path(temp_dir) / "Raindrop" / ".raindian-usage.jsonl"
            record = json.loads(usage_path.read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        self.assertEqual(record["raindrop_id"], 123)
        self.assertEqual(record["operation"], "summarize")
        self.assertEqual(record["model"], "gpt-5.6-luna")
        self.assertEqual(record["input_tokens"], 120)
        self.assertEqual(record["cached_input_tokens"], 20)
        self.assertEqual(record["output_tokens"], 34)
        self.assertEqual(record["reasoning_tokens"], 8)
        self.assertEqual(record["total_tokens"], 154)
        self.assertEqual(record["price_source"], "official")
        self.assertEqual(record["input_per_million_usd"], 0.2)
        self.assertEqual(record["cached_input_per_million_usd"], 0.02)
        self.assertEqual(record["output_per_million_usd"], 1.2)
        self.assertEqual(record["estimated_cost_usd"], 0.0000612)
        self.assertNotIn("url", record)
        self.assertNotIn("content", record)

    def test_sync_raindrop_summaries_updates_only_the_managed_note_block(self) -> None:
        with TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "Raindrop"
            destination.mkdir()
            note = destination / "Japanese title - 123.md"
            note.write_text(
                "---\nraindrop_id: \"123\"\nsummary_model: \"gpt-5.6-luna\"\n---\n\n"
                "# Japanese title\n\n## Summary\n\n日本語の要約です。\n",
                encoding="utf-8",
            )
            config = Config(vault_path=temp_dir, sleep_seconds=0)
            args = parse_args(["--sync-raindrop-summary"])
            client = Mock()
            client.get_raindrop.return_value = {"note": "My manual note"}

            result = sync_raindrop_summaries(config, args, client)

        self.assertEqual(result, 0)
        client.update_raindrop_note.assert_called_once()
        updated_note = client.update_raindrop_note.call_args.args[1]
        self.assertIn("My manual note", updated_note)
        self.assertIn("日本語の要約です。", updated_note)

    def test_sync_raindrop_summaries_dry_run_does_not_call_raindrop(self) -> None:
        with TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "Raindrop"
            destination.mkdir()
            (destination / "Japanese title - 123.md").write_text(
                "---\nraindrop_id: \"123\"\nsummary_model: \"gpt-5.6-luna\"\n---\n\n"
                "## Summary\n\n日本語の要約です。\n",
                encoding="utf-8",
            )
            config = Config(vault_path=temp_dir, sleep_seconds=0)
            args = parse_args(["--sync-raindrop-summary", "--dry-run"])
            client = Mock()

            result = sync_raindrop_summaries(config, args, client)

        self.assertEqual(result, 0)
        client.get_raindrop.assert_not_called()
        client.update_raindrop_note.assert_not_called()

    def test_sync_raindrop_tags_appends_only_missing_non_base_tags(self) -> None:
        with TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "Raindrop"
            destination.mkdir()
            (destination / "Japanese title - 123.md").write_text(
                "---\nraindrop_id: \"123\"\nraindrop_collection_id: \"5\"\n"
                "summary_model: \"gpt-5.6-luna\"\ntags:\n  - \"raindrop\"\n"
                "  - \"bookmark\"\n  - \"existing\"\n  - \"新規タグ\"\n"
                "  - \"AI\"\n  - \"X\"\n  - \"SNS\"\n---\n",
                encoding="utf-8",
            )
            config = Config(vault_path=temp_dir, sleep_seconds=0)
            args = parse_args(["--sync-raindrop-tags"])
            client = Mock()
            client.get_raindrop.return_value = {"tags": ["existing"]}

            result = sync_raindrop_tags(config, args, client)

        self.assertEqual(result, 0)
        client.append_raindrop_tags.assert_called_once_with(5, 123, ["新規タグ", "ai"])

    def test_sync_raindrop_tags_dry_run_does_not_call_raindrop(self) -> None:
        with TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "Raindrop"
            destination.mkdir()
            (destination / "Japanese title - 123.md").write_text(
                "---\nraindrop_id: \"123\"\nraindrop_collection_id: \"5\"\n"
                "summary_model: \"gpt-5.6-luna\"\nllm_tags:\n  - \"新規タグ\"\n---\n",
                encoding="utf-8",
            )
            config = Config(vault_path=temp_dir, sleep_seconds=0)
            args = parse_args(["--sync-raindrop-tags", "--dry-run"])
            client = Mock()

            result = sync_raindrop_tags(config, args, client)

        self.assertEqual(result, 0)
        client.get_raindrop.assert_not_called()
        client.append_raindrop_tags.assert_not_called()

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
                patch("raindian.__main__.count_prompt_tokens", return_value=(10, None)),
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
        self.assertIn("price_source=official", output.getvalue())
        self.assertIn("assumed_output_tokens=10 (input-matched)", output.getvalue())

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

    def test_estimate_uses_matching_usage_records_for_typical_output(self) -> None:
        items = [{"_id": 1, "title": "First", "link": "https://example.com/first", "collection": {}}]
        page = PageFetchResult(url=items[0]["link"], text="Example page text.", title="Example", error=None)
        usage_records = [
            {"model": "gpt-5.6-luna", "reasoning_effort": "none", "input_tokens": 200, "output_tokens": 50},
            {"model": "gpt-5.6-luna", "reasoning_effort": "none", "input_tokens": 200, "output_tokens": 50},
        ]

        with TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "Raindrop"
            destination.mkdir()
            usage_path = destination / ".raindian-usage.jsonl"
            usage_path.write_text(
                "".join(json.dumps(record) + "\n" for record in usage_records),
                encoding="utf-8",
            )
            config = Config(vault_path=temp_dir, sleep_seconds=0)
            args = parse_args(["--estimate", "--estimate-sample-size", "1"])
            output = StringIO()
            with (
                patch.dict(os.environ, {"RAINDROP_TOKEN": "token"}, clear=True),
                patch("raindian.__main__.RaindropClient.iter_raindrops", return_value=iter(items)),
                patch("raindian.__main__.fetch_page_text", return_value=page),
                patch("raindian.__main__.count_prompt_tokens", return_value=(100, None)),
                redirect_stdout(output),
            ):
                result = estimate_bookmarks(config, args)

        self.assertEqual(result, 0)
        self.assertIn("assumed_output_tokens=25 (usage-ratio records=2)", output.getvalue())

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
