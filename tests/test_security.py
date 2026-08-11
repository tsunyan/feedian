import socket
import unittest
from unittest.mock import Mock, patch

from feedian.extract import SafeRedirectHandler, fetch_page_text, validate_fetch_url


class FetchSecurityTests(unittest.TestCase):
    @patch("feedian.extract.socket.getaddrinfo")
    def test_public_http_url_is_allowed(self, getaddrinfo) -> None:
        getaddrinfo.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

        validate_fetch_url("https://example.com/article")

    def test_non_http_scheme_is_blocked_before_fetching(self) -> None:
        result = fetch_page_text("file:///C:/secret.txt", timeout_seconds=1, max_chars=1000)

        self.assertIn("only http and https", result.error or "")

    @patch("feedian.extract.build_opener")
    @patch("feedian.extract.socket.getaddrinfo")
    def test_public_url_is_fetched_through_safe_opener(self, getaddrinfo, build_opener) -> None:
        getaddrinfo.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        response = Mock()
        response.headers.get.return_value = "text/html; charset=utf-8"
        response.read.return_value = b"<article><p>Public content</p></article>"
        build_opener.return_value.open.return_value.__enter__.return_value = response

        result = fetch_page_text("https://example.com/article", timeout_seconds=1, max_chars=1000)

        self.assertEqual(result.text, "Public content")
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


if __name__ == "__main__":
    unittest.main()
