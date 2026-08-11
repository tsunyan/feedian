import socket
import unittest
from unittest.mock import Mock, patch
from urllib.request import Request

from feedian.extract import (
    MAX_HTML_BYTES,
    ExtractedPageParts,
    SafeRedirectHandler,
    fetch_page_text,
    validate_fetch_url,
)


class FetchSecurityTests(unittest.TestCase):
    @patch("feedian.extract.socket.getaddrinfo")
    def test_public_http_url_is_allowed(self, getaddrinfo) -> None:
        getaddrinfo.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

        validate_fetch_url("https://example.com/article")

    def test_non_http_scheme_is_blocked_before_fetching(self) -> None:
        result = fetch_page_text("file:///C:/secret.txt", timeout_seconds=1, max_chars=1000)

        self.assertIn("only http and https", result.error or "")

    @patch(
        "feedian.extract.render_html_with_browser",
        return_value=(
            "<article>" + ("Public content with enough article detail. " * 20) + "</article>",
            "https://example.com/article",
            "Public page",
        ),
    )
    @patch(
        "feedian.extract.extract_page_parts",
        return_value=ExtractedPageParts("Public content " * 30, "", "trafilatura"),
    )
    @patch("feedian.extract.build_opener")
    @patch("feedian.extract.socket.getaddrinfo")
    def test_public_url_is_fetched_through_safe_opener(
        self, getaddrinfo, build_opener, extract_html, render_browser
    ) -> None:
        getaddrinfo.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        response = Mock()
        response.headers.get.return_value = "text/html; charset=utf-8"
        response.read.return_value = (
            b"<article><p>Public content with enough article detail for reliable extraction. " * 10
            + b"</p></article>"
        )
        build_opener.return_value.open.return_value.__enter__.return_value = response

        result = fetch_page_text("https://example.com/article", timeout_seconds=1, max_chars=1000)

        self.assertIn("Public content", result.text)
        response.read.assert_called_once_with(MAX_HTML_BYTES + 1)
        build_opener.return_value.open.assert_called_once()

    @patch("feedian.extract.socket.getaddrinfo")
    def test_private_address_is_blocked(self, getaddrinfo) -> None:
        getaddrinfo.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]

        with self.assertRaisesRegex(ValueError, "non-public address"):
            validate_fetch_url("http://localhost:8080")

    @patch("feedian.extract.socket.getaddrinfo")
    def test_redirect_destination_is_checked(self, getaddrinfo) -> None:
        getaddrinfo.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", 80))]
        handler = SafeRedirectHandler(allow_private_urls=False)

        with self.assertRaisesRegex(ValueError, "non-public address"):
            handler.redirect_request(
                type("Request", (), {"full_url": "https://example.com/article"})(),
                None,
                302,
                "Found",
                {},
                "http://192.168.1.1/admin",
            )

    @patch("feedian.extract.validate_fetch_url")
    def test_permanent_redirect_preserves_get_and_validates_destination(self, validate_fetch_url) -> None:
        handler = SafeRedirectHandler(allow_private_urls=False)
        request = Request("https://example.com/old", method="GET")

        redirected = handler.redirect_request(
            request,
            None,
            308,
            "Permanent Redirect",
            {},
            "/new",
        )

        validate_fetch_url.assert_called_once_with(
            "https://example.com/new", allow_private_urls=False
        )
        self.assertEqual(redirected.full_url, "https://example.com/new")
        self.assertEqual(redirected.get_method(), "GET")


if __name__ == "__main__":
    unittest.main()
