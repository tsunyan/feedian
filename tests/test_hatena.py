import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from feedian.hatena import (
    clean_hatena_excerpt,
    fetch_hatena_bookmarks,
    fetch_hatena_bookmark_counts,
    fetch_hatena_entry_discussion,
    load_hatena_export,
)


class HatenaExportTests(unittest.TestCase):
    def test_bookmark_count_api_batches_urls_and_keeps_zeroes(self) -> None:
        urls = ["https://example.com/a", "https://example.com/b"]
        with patch(
            "feedian.hatena._read_entry_json",
            return_value={urls[0]: 12, urls[1]: 0},
        ) as read_json:
            counts = fetch_hatena_bookmark_counts(urls, workers=1)

        request = read_json.call_args.args[0]
        self.assertIn("count/entries?", request.full_url)
        self.assertEqual(counts, {urls[0]: 12, urls[1]: 0})

    def test_niconico_excerpt_removes_job_box_advertisement(self) -> None:
        excerpt = (
            "記事の導入 sponsored by 求人ボックス もっと見る > "
            "Webデザイナー 年収800万円 概要確認状況経緯"
        )

        cleaned = clean_hatena_excerpt("https://dic.nicovideo.jp/a/example", excerpt)

        self.assertEqual(cleaned, "記事の導入 概要確認状況経緯")

    def test_niconico_excerpt_cleanup_does_not_apply_to_other_domains(self) -> None:
        excerpt = "本文 sponsored by 求人ボックス 求人情報 概要"

        cleaned = clean_hatena_excerpt("https://example.com/article", excerpt)

        self.assertEqual(cleaned, excerpt)

    def test_entry_discussion_preserves_public_comment_authors_tags_and_times(self) -> None:
        response = {
            "count": 12,
            "entry_url": "https://b.hatena.ne.jp/entry/s/example.com/article",
            "bookmarks": [
                {
                    "user": "alice",
                    "tags": ["python", "参考"],
                    "timestamp": "2026/08/11 12:34",
                    "comment": "人間が書いたコメント",
                },
                {"user": "bob", "tags": [], "timestamp": "", "comment": ""},
            ],
        }
        with patch("feedian.hatena._read_entry_json", return_value=response) as read_json:
            discussion = fetch_hatena_entry_discussion("https://example.com/article")

        self.assertIn("jsonlite/?url=https%3A%2F%2Fexample.com%2Farticle", read_json.call_args.args[0].full_url)
        self.assertEqual(discussion.bookmark_count, 12)
        self.assertEqual(discussion.entry_url, "https://b.hatena.ne.jp/entry/s/example.com/article")
        self.assertEqual(len(discussion.comments), 1)
        self.assertEqual(discussion.comments[0].user, "alice")
        self.assertEqual(discussion.comments[0].tags, ["python", "参考"])
    def test_authenticated_search_exports_private_bookmarks(self) -> None:
        response = {
            "bookmarks": [
                {
                    "entry": {
                        "title": "Private item",
                        "url": "https://example.com/private",
                        "snippet": "本文抜粋",
                    },
                    "timestamp": 1722470400,
                    "comment": "[python][あとで読む]秘密のコメント",
                    "is_private": 1,
                },
                {
                    "entry": {"title": "Public item", "url": "https://example.com/public"},
                    "timestamp": 1722470500,
                    "comment": "公開コメント",
                    "is_private": "0",
                },
            ],
            "meta": {"total": 2},
        }
        progress: list[tuple[int, int]] = []
        with (
            patch("feedian.hatena.SEARCH_QUERIES", ("https",)),
            patch("feedian.hatena._read_json", return_value=response) as read_json,
        ):
            items = fetch_hatena_bookmarks(
                "hatena-user",
                "secret-api-key",
                limit=2,
                request_interval_seconds=0,
                on_page=lambda collected, total: progress.append((collected, total)),
            )

        request = read_json.call_args.args[0]
        self.assertIn("q=https&of=0&limit=2", request.full_url)
        self.assertIn('Username="hatena-user"', request.headers["X-wsse"])
        self.assertNotIn("secret-api-key", request.headers["X-wsse"])
        self.assertEqual(progress, [(2, 2)])
        private_item = next(item for item in items if item.private)
        public_item = next(item for item in items if not item.private)
        self.assertEqual(private_item.tags, ["python", "あとで読む"])
        self.assertEqual(private_item.comment, "秘密のコメント")
        self.assertFalse(public_item.private)

    def test_authenticated_search_combines_http_and_https_queries(self) -> None:
        responses = [
            {
                "bookmarks": [{"entry": {"title": "HTTPS", "url": "https://example.com/a"}}],
                "meta": {"status": 200, "total": 1},
            },
            {
                "bookmarks": [
                    {"entry": {"title": "duplicate", "url": "https://example.com/a"}},
                    {"entry": {"title": "HTTP", "url": "http://example.com/b"}},
                ],
                "meta": {"status": 200, "total": 2},
            },
        ]
        with patch("feedian.hatena._read_json", side_effect=responses) as read_json:
            items = fetch_hatena_bookmarks("user", "key", request_interval_seconds=0)

        urls = [call.args[0].full_url for call in read_json.call_args_list]
        self.assertIn("q=https", urls[0])
        self.assertIn("q=http", urls[1])
        self.assertEqual([item.url for item in items], ["https://example.com/a", "http://example.com/b"])

    def test_authenticated_search_rejects_json_authentication_error(self) -> None:
        response = {"bookmarks": [], "meta": {"status": 403, "total": 0}}
        with patch("feedian.hatena._read_json", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "authentication failed"):
                fetch_hatena_bookmarks("user", "wrong", request_interval_seconds=0)

    def test_atom_export_preserves_comment_tags_and_private_flag(self) -> None:
        atom = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:dc="http://purl.org/dc/elements/1.1/"
      xmlns:hatena="http://www.hatena.ne.jp/info/xmlns#">
  <entry>
    <title>Example article</title>
    <link rel="related" href="https://example.com/article#top" />
    <published>2026-08-01T12:00:00Z</published>
    <summary>自分のコメント</summary>
    <dc:subject>Python</dc:subject>
    <dc:subject>あとで読む</dc:subject>
    <hatena:private>true</hatena:private>
  </entry>
</feed>"""
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bookmarks.atom"
            path.write_text(atom, encoding="utf-8")
            items = load_hatena_export(str(path))

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].source, "hatena")
        self.assertEqual(items[0].title, "Example article")
        self.assertEqual(items[0].comment, "自分のコメント")
        self.assertEqual(items[0].tags, ["Python", "あとで読む"])
        self.assertTrue(items[0].private)
        self.assertTrue(items[0].source_id.startswith("hatena-"))
        self.assertTrue(items[0].content_key.startswith("url:"))

    def test_rss_export_is_supported(self) -> None:
        rss = """<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:dc="http://purl.org/dc/elements/1.1/">
  <item rdf:about="https://example.com/rss">
    <title>RSS item</title><link>https://example.com/rss</link>
    <description>RSS comment</description><dc:subject>rss</dc:subject>
  </item>
</rdf:RDF>"""
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bookmarks.rdf"
            path.write_text(rss, encoding="utf-8")
            items = load_hatena_export(str(path))

        self.assertEqual([(item.title, item.comment, item.tags) for item in items], [("RSS item", "RSS comment", ["rss"])])

    def test_bookmark_html_is_supported(self) -> None:
        html = """<!DOCTYPE NETSCAPE-Bookmark-file-1>
<DL><p><DT><A HREF="https://example.com/html" ADD_DATE="1722470400" TAGS="web,あとで読む" PRIVATE="1">HTML item</A>
<DD>HTML comment</DL>"""
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bookmarks.html"
            path.write_text(html, encoding="utf-8")
            items = load_hatena_export(str(path))

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].comment, "HTML comment")
        self.assertEqual(items[0].tags, ["web", "あとで読む"])
        self.assertTrue(items[0].private)


if __name__ == "__main__":
    unittest.main()
