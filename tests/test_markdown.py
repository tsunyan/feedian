import unittest

from raindian.markdown import merge_tags, note_filename, sanitize_filename


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

    def test_merge_tags_normalizes_and_deduplicates(self) -> None:
        self.assertEqual(
            merge_tags(["Raindrop"], ["AI Tools", "#raindrop"], ["日本語 タグ"]),
            ["raindrop", "ai-tools", "日本語-タグ"],
        )

if __name__ == "__main__":
    unittest.main()
