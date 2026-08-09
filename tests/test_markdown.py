import unittest

from raindian.extract import PageFetchResult
from raindian.markdown import (
    merge_tags,
    note_filename,
    render_note,
    sanitize_filename,
    upsert_raindrop_summary,
)


class MarkdownTests(unittest.TestCase):
    def test_sanitize_filename_removes_windows_separators(self) -> None:
        self.assertEqual(sanitize_filename('A/B:C*"D?'), "A B C D")

    def test_note_filename_includes_id(self) -> None:
        item = {"_id": 123, "title": "Example / Bookmark"}
        self.assertEqual(note_filename(item), "Example Bookmark - 123.md")

    def test_note_filename_is_short_and_does_not_end_with_space(self) -> None:
        item = {"_id": 123, "title": "長いタイトル " * 20}
        filename = note_filename(item)
        self.assertLessEqual(len(filename.split(" - ", 1)[0]), 60)
        self.assertFalse(filename.split(" - ", 1)[0].endswith((" ", ".")))

    def test_note_filename_can_use_the_japanese_note_title(self) -> None:
        item = {"_id": 123, "title": "Original foreign title"}

        self.assertEqual(note_filename(item, title="日本語のノートタイトル"), "日本語のノートタイトル - 123.md")

    def test_merge_tags_normalizes_and_deduplicates(self) -> None:
        self.assertEqual(
            merge_tags(["Raindrop"], ["AI Tools", "#raindrop"], ["日本語 タグ"]),
            ["raindrop", "ai-tools", "日本語-タグ"],
        )

    def test_render_note_preserves_original_excerpt_and_extracted_content(self) -> None:
        item = {
            "_id": 123,
            "title": "Original title",
            "link": "https://example.com/article",
            "excerpt": "Original excerpt",
            "collection": {"$id": 5},
        }
        page = PageFetchResult(
            url=item["link"],
            title="Original page title",
            text="Original extracted body.",
            error=None,
        )
        summary = {
            "note_title": "日本語のタイトル",
            "summary": "日本語の要約です。",
            "key_points": [],
            "tags": ["AI"],
        }

        rendered = render_note(
            item=item,
            page=page,
            summary=summary,
            base_tags=[],
            generated_at="2026-08-09T00:00:00+00:00",
            model="gpt-5.6-luna",
        )

        self.assertIn('title: "日本語のタイトル"', rendered)
        self.assertIn('source_title: "Original title"', rendered)
        self.assertIn("llm_tags:", rendered)
        self.assertIn("## Summary\n\n日本語の要約です。", rendered)
        self.assertIn("### Excerpt (Original)\n\nOriginal excerpt", rendered)
        self.assertIn("## Extracted Content (Original)\n\nOriginal extracted body.", rendered)

    def test_upsert_raindrop_summary_preserves_manual_note_and_replaces_managed_block(self) -> None:
        original = "My manual note"

        added = upsert_raindrop_summary(original, "最初の要約")
        replaced = upsert_raindrop_summary(added, "更新後の要約")

        self.assertIn("My manual note", replaced)
        self.assertIn("更新後の要約", replaced)
        self.assertNotIn("最初の要約", replaced)
        self.assertEqual(replaced.count("<!-- raindian:summary:start -->"), 1)

if __name__ == "__main__":
    unittest.main()
