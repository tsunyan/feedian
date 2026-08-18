import socket
import ssl
import unittest
from io import BytesIO
from urllib.error import HTTPError, URLError

from unittest.mock import patch

from pypdf import PdfWriter

from feedian.extract import (
    TextExtractor,
    UnresolvableHostError,
    clean_extracted_text,
    decode_html,
    extract_html,
    extract_page_parts,
    fetch_page_text,
    resolve_content_url,
    should_render_with_browser,
    should_use_recall_fallback,
)


class TextExtractorTests(unittest.TestCase):
    def extract(self, html: str) -> TextExtractor:
        parser = TextExtractor()
        parser.feed(html)
        parser.close()
        return parser

    def test_article_is_preferred_and_surrounding_content_is_removed(self) -> None:
        parser = self.extract(
            "<html><head><title>Page title</title></head><body>"
            "<header>Site header</header><nav>Menu</nav><article>"
            "<h1>Article heading</h1><p>Important body text.</p>"
            "<div class='related-posts'>Related item</div><div class='comments'>Comment</div>"
            "</article><footer>Footer</footer></body></html>"
        )

        self.assertEqual(parser.title, "Page title")
        self.assertIn("Article heading", parser.text)
        self.assertIn("Important body text.", parser.text)
        for unwanted in ("Site header", "Menu", "Related item", "Comment", "Footer"):
            self.assertNotIn(unwanted, parser.text)

    def test_main_is_used_when_article_is_missing(self) -> None:
        parser = self.extract(
            "<body><div class='cookie-banner'>Cookie notice</div><main>"
            "<p>Main content.</p></main><aside>Recommended links</aside></body>"
        )

        self.assertEqual(parser.text, "Main content.")

    def test_content_class_is_used_as_a_fallback_candidate(self) -> None:
        parser = self.extract(
            "<body><div id='site-navigation'>Links</div>"
            "<div class='post-content'><p>Fallback article text.</p></div>"
            "<div class='ad'>Advertisement</div></body>"
        )

        self.assertEqual(parser.text, "Fallback article text.")

    def test_nested_equal_candidates_do_not_corrupt_active_candidate_tracking(self) -> None:
        parser = self.extract(
            "<body><span class='content'><span class='content'>first</span> second</span></body>"
        )

        self.assertEqual(parser.text, "first second")


class StagedExtractionTests(unittest.TestCase):
    def test_pdf_bytes_are_preserved_when_text_is_not_extractable(self) -> None:
        output = BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.write(output)
        raw = output.getvalue()

        result = self._fetch_static_response(raw, "application/pdf")

        self.assertEqual(result.raw_body, raw)
        self.assertEqual(result.extraction_method, "pypdf")
        self.assertIn("OCR", result.error or "")

    @staticmethod
    def _fetch_static_response(raw: bytes, content_type: str):
        class Response(BytesIO):
            status = 200
            headers = {"Content-Type": content_type}

            def geturl(self):
                return "https://example.com/document"

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        class Opener:
            def open(self, *_args, **_kwargs):
                return Response(raw)

        with patch("feedian.extract.validate_fetch_url"), patch("feedian.extract.build_opener", return_value=Opener()):
            return fetch_page_text("https://example.com/document", timeout_seconds=1, max_chars=1000)

    def test_decode_html_honors_shift_jis_meta(self) -> None:
        html = '<meta charset="Shift_JIS"><article>日本語の本文です。</article>'.encode("cp932")

        encoding, decoded = decode_html(html, "text/html")

        self.assertIn(encoding.lower(), {"cp932", "shift_jis", "windows-31j"})
        self.assertIn("日本語の本文です。", decoded)
        self.assertNotIn("\ufffd", decoded)

    def test_megalodon_url_is_resolved_to_archive_content(self) -> None:
        self.assertEqual(
            resolve_content_url("https://megalodon.jp/2024-0101-0000/example.com/page"),
            "https://megalodon.jp/ref/2024-0101-0000/example.com/page",
        )

    def test_trafilatura_extracts_markdown_article(self) -> None:
        body = "本文です。" * 100
        text, title, method = extract_html(
            f"<html><head><title>題名</title></head><body><nav>menu</nav><article><h1>見出し</h1><p>{body}</p></article></body></html>",
            "https://example.com/article",
        )

        self.assertIn("見出し", text)
        self.assertIn(body, text)
        self.assertIn(title, {"題名", "見出し"})
        self.assertEqual(method, "trafilatura")

    def test_short_titled_article_does_not_trigger_noisy_browser_replacement(self) -> None:
        text = "# Short report\n\nThis is a short but complete report with enough factual detail to stand alone."
        html = '<div id="app"></div><article><h1>Short report</h1></article>'

        self.assertFalse(should_render_with_browser(text, html, "Short report"))

    def test_anond_main_text_and_replies_are_separated(self) -> None:
        html = """<html><head><title>増田</title></head><body><div class="day">
<div class="body"><div class="section"><h3>本文題名</h3><p>本文です。</p>
<div class="share-button">ツイート シェア</div></div></div>
<div class="refererlist"><ul><li><div>返信その1</div></li><li><div>返信その2</div></li></ul></div>
</div></body></html>"""

        parts = extract_page_parts(html, "https://anond.hatelabo.jp/20260811123456")

        self.assertIn("本文です。", parts.text)
        self.assertNotIn("ツイート", parts.text)
        self.assertIn("返信その1", parts.discussion_text)
        self.assertNotIn("返信その1", parts.text)
        self.assertEqual(parts.method, "anond-dom")

    def test_recall_is_used_only_when_precision_is_catastrophically_short(self) -> None:
        self.assertTrue(should_use_recall_fallback("short" * 10, "full article " * 100))
        self.assertFalse(should_use_recall_fallback("valid article " * 20, "page text " * 100))

    def test_standalone_advertisement_lines_are_removed(self) -> None:
        text = "First paragraph.\n\nAdvertisement\n\nSecond paragraph."

        cleaned = clean_extracted_text(text, "https://www.gizmodo.jp/article/example/")

        self.assertEqual(cleaned, "First paragraph.\n\nSecond paragraph.")

    def test_47news_job_section_is_removed(self) -> None:
        text = (
            "News article body.\n\n"
            "## ピックアップ求人情報\n\n"
            "Software engineer\n\nSponsored by example"
        )

        cleaned = clean_extracted_text(text, "https://www.47news.jp/123.html")

        self.assertEqual(cleaned, "News article body.")

    def test_47news_cleanup_does_not_apply_to_other_domains(self) -> None:
        text = "Article.\n\n## ピックアップ求人情報\n\nQuoted as part of the article."

        cleaned = clean_extracted_text(text, "https://example.com/article")

        self.assertEqual(cleaned, text)

    @patch("feedian.extract.render_html_with_browser")
    @patch("feedian.extract.build_opener")
    @patch("feedian.extract.validate_fetch_url")
    def test_low_confidence_static_html_uses_better_browser_result(
        self, validate_url, build_opener, render_browser
    ) -> None:
        response = build_opener.return_value.open.return_value.__enter__.return_value
        response.headers.get.return_value = "text/html; charset=utf-8"
        response.read.return_value = b"<html><body><div id='app'></div></body></html>"
        response.geturl.return_value = "https://example.com/article"
        rendered_body = "Rendered article body. " * 30
        render_browser.return_value = (
            f"<html><head><title>Rendered</title></head><body><article>{rendered_body}</article></body></html>",
            "https://example.com/article",
            "Rendered",
        )

        result = fetch_page_text("https://example.com/article", 5, 1000)

        self.assertEqual(result.fetch_method, "browser")
        self.assertIn("Rendered article body", result.text)
        self.assertEqual(result.error, None)

    @patch("feedian.extract.fetch_page_text_with_browser")
    @patch("feedian.extract.build_opener")
    @patch("feedian.extract.validate_fetch_url")
    def test_http_403_uses_browser_fallback(self, validate_url, build_opener, browser_fetch) -> None:
        build_opener.return_value.open.side_effect = HTTPError(
            "https://example.com/article", 403, "Forbidden", {}, BytesIO()
        )
        browser_fetch.return_value = type("Result", (), {"text": "Browser article"})()

        result = fetch_page_text("https://example.com/article", 5, 1000)

        self.assertEqual(result.text, "Browser article")
        browser_fetch.assert_called_once()

    @patch("feedian.extract.build_opener")
    @patch("feedian.extract.validate_fetch_url")
    def test_http_404_reports_status_and_error(self, validate_url, build_opener) -> None:
        build_opener.return_value.open.side_effect = HTTPError(
            "https://example.com/article", 404, "Not Found", {}, BytesIO()
        )

        result = fetch_page_text("https://example.com/article", 5, 1000)

        self.assertEqual(result.http_status, 404)
        self.assertTrue(result.error)

    @patch("feedian.extract.fetch_page_text_with_browser")
    @patch("feedian.extract.build_opener")
    @patch("feedian.extract.validate_fetch_url")
    def test_http_403_with_failed_browser_fallback_reports_origin_status(
        self, validate_url, build_opener, browser_fetch
    ) -> None:
        build_opener.return_value.open.side_effect = HTTPError(
            "https://example.com/article", 403, "Forbidden", {}, BytesIO()
        )
        browser_fetch.side_effect = Exception("browser boom")

        result = fetch_page_text("https://example.com/article", 5, 1000)

        self.assertEqual(result.http_status, 403)
        self.assertIn("browser fallback failed", result.error or "")

    @patch("feedian.extract.validate_fetch_url")
    def test_blocked_url_leaves_status_unset(self, validate_url) -> None:
        validate_url.side_effect = ValueError("private address")

        result = fetch_page_text("https://example.com/article", 5, 1000)

        self.assertIsNone(result.http_status)
        self.assertIn("blocked URL", result.error or "")

    @patch("feedian.extract.build_opener")
    @patch("feedian.extract.validate_fetch_url")
    def test_connection_failure_leaves_status_unset(self, validate_url, build_opener) -> None:
        build_opener.return_value.open.side_effect = URLError("Name or service not known")

        result = fetch_page_text("https://example.com/article", 5, 1000)

        self.assertIsNone(result.http_status)

    @patch("feedian.extract.build_opener")
    @patch("feedian.extract.validate_fetch_url")
    def test_successful_fetch_still_reports_status(self, validate_url, build_opener) -> None:
        response = build_opener.return_value.open.return_value.__enter__.return_value
        response.headers.get.return_value = "text/html; charset=utf-8"
        response.read.return_value = b"<html><body><article><p>Some article body text.</p></article></body></html>"
        response.geturl.return_value = "https://example.com/article"
        response.status = 200

        result = fetch_page_text("https://example.com/article", 5, 1000)

        self.assertEqual(result.http_status, 200)

    @patch("feedian.extract.socket.getaddrinfo")
    def test_dns_unresolvable_host_sets_dns_failure_kind(self, getaddrinfo) -> None:
        getaddrinfo.side_effect = socket.gaierror("not known")

        result = fetch_page_text("https://dead.example.com/article", 5, 1000)

        self.assertEqual(result.failure_kind, "dns")
        self.assertIn("blocked URL", result.error or "")

    def test_private_address_rejection_leaves_failure_kind_none(self) -> None:
        # 127.0.0.1 resolves locally without a network call, so this exercises
        # the SSRF guard's ValueError branch, not the DNS failure branch.
        result = fetch_page_text("http://127.0.0.1/secret", 5, 1000)

        self.assertIsNone(result.failure_kind)
        self.assertIn("blocked URL", result.error or "")

    def test_scheme_violation_leaves_failure_kind_none(self) -> None:
        result = fetch_page_text("ftp://example.com/article", 5, 1000)

        self.assertIsNone(result.failure_kind)

    def test_missing_hostname_leaves_failure_kind_none(self) -> None:
        result = fetch_page_text("https:///no-host", 5, 1000)

        self.assertIsNone(result.failure_kind)

    @patch("feedian.extract.build_opener")
    @patch("feedian.extract.validate_fetch_url")
    def test_redirect_destination_dns_failure_does_not_set_failure_kind(
        self, validate_url, build_opener
    ) -> None:
        # Only the pre-validation check in fetch_page_text sets "dns". A DNS
        # failure raised while following a redirect (SafeRedirectHandler,
        # inside opener.open) must not resurrect the same failure_kind.
        build_opener.return_value.open.side_effect = UnresolvableHostError(
            "hostname could not be resolved: redirect.example.com"
        )

        result = fetch_page_text("https://example.com/article", 5, 1000)

        self.assertIsNone(result.failure_kind)

    @patch("feedian.extract.build_opener")
    @patch("feedian.extract.validate_fetch_url")
    def test_opener_open_timeout_sets_timeout_failure_kind(self, validate_url, build_opener) -> None:
        build_opener.return_value.open.side_effect = URLError(TimeoutError("timed out"))

        result = fetch_page_text("https://example.com/article", 5, 1000)

        self.assertEqual(result.failure_kind, "timeout")

    @patch("feedian.extract.build_opener")
    @patch("feedian.extract.validate_fetch_url")
    def test_response_read_timeout_sets_timeout_failure_kind(self, validate_url, build_opener) -> None:
        response = build_opener.return_value.open.return_value.__enter__.return_value
        response.headers.get.return_value = "text/html; charset=utf-8"
        response.read.side_effect = TimeoutError("timed out")

        result = fetch_page_text("https://example.com/article", 5, 1000)

        self.assertEqual(result.failure_kind, "timeout")

    @patch("feedian.extract.build_opener")
    @patch("feedian.extract.validate_fetch_url")
    def test_ssl_failure_leaves_failure_kind_none(self, validate_url, build_opener) -> None:
        build_opener.return_value.open.side_effect = ssl.SSLError("bad handshake")

        result = fetch_page_text("https://example.com/article", 5, 1000)

        self.assertIsNone(result.failure_kind)

    @patch("feedian.extract.build_opener")
    @patch("feedian.extract.validate_fetch_url")
    def test_http_error_leaves_failure_kind_none(self, validate_url, build_opener) -> None:
        build_opener.return_value.open.side_effect = HTTPError(
            "https://example.com/article", 500, "Server Error", {}, BytesIO()
        )

        result = fetch_page_text("https://example.com/article", 5, 1000)

        self.assertIsNone(result.failure_kind)

    @patch("feedian.extract.fetch_page_text_with_browser")
    @patch("feedian.extract.build_opener")
    @patch("feedian.extract.validate_fetch_url")
    def test_browser_fallback_receives_browser_timeout_not_http_timeout(
        self, validate_url, build_opener, browser_fetch
    ) -> None:
        build_opener.return_value.open.side_effect = HTTPError(
            "https://example.com/article", 403, "Forbidden", {}, BytesIO()
        )
        browser_fetch.return_value = type("Result", (), {"text": "Browser article"})()

        fetch_page_text("https://example.com/article", 5, 1000, browser_timeout_seconds=45)

        self.assertEqual(browser_fetch.call_args.kwargs["timeout_seconds"], 45)

    @patch("feedian.extract.fetch_page_text_with_browser")
    @patch("feedian.extract.build_opener")
    @patch("feedian.extract.validate_fetch_url")
    def test_browser_fallback_defaults_to_30_second_timeout(
        self, validate_url, build_opener, browser_fetch
    ) -> None:
        build_opener.return_value.open.side_effect = HTTPError(
            "https://example.com/article", 403, "Forbidden", {}, BytesIO()
        )
        browser_fetch.return_value = type("Result", (), {"text": "Browser article"})()

        fetch_page_text("https://example.com/article", 5, 1000)

        self.assertEqual(browser_fetch.call_args.kwargs["timeout_seconds"], 30)

    @patch("feedian.extract.render_html_with_browser")
    @patch("feedian.extract.build_opener")
    @patch("feedian.extract.validate_fetch_url")
    def test_low_quality_html_render_receives_browser_timeout(
        self, validate_url, build_opener, render_browser
    ) -> None:
        response = build_opener.return_value.open.return_value.__enter__.return_value
        response.headers.get.return_value = "text/html; charset=utf-8"
        response.read.return_value = b"<html><body><div id='app'></div></body></html>"
        response.geturl.return_value = "https://example.com/article"
        rendered_body = "Rendered article body. " * 30
        render_browser.return_value = (
            f"<html><head><title>Rendered</title></head><body><article>{rendered_body}</article></body></html>",
            "https://example.com/article",
            "Rendered",
        )

        fetch_page_text("https://example.com/article", 5, 1000, browser_timeout_seconds=45)

        self.assertEqual(render_browser.call_args.kwargs["timeout_seconds"], 45)


if __name__ == "__main__":
    unittest.main()
