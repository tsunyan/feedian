import socket
import unittest
from unittest.mock import Mock, patch
from urllib.request import Request

from feedian.extract import (
    BrowserCandidate,
    ExtractedPageParts,
    SafeRedirectHandler,
    _validated_create_connection,
    _ValidatingHTTPConnection,
    _ValidatingHTTPHandler,
    _ValidatingHTTPSConnection,
    _ValidatingHTTPSHandler,
    fetch_page_text,
    render_html_with_browser,
    validate_fetch_url,
)
from feedian.vault import FetchPolicy, NetworkPolicy


def _policy(**overrides) -> FetchPolicy:
    defaults: dict = dict(
        network=NetworkPolicy(allowed_private_hosts=frozenset()),
        html_max_bytes=10 * 1024 * 1024,
        document_max_bytes=100 * 1024 * 1024,
        timeout_seconds=5,
        browser_timeout_seconds=30,
    )
    defaults.update(overrides)
    return FetchPolicy(**defaults)


class FetchSecurityTests(unittest.TestCase):
    @patch("feedian.extract.socket.getaddrinfo")
    def test_public_http_url_is_allowed(self, getaddrinfo) -> None:
        getaddrinfo.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

        validate_fetch_url("https://example.com/article")

    def test_non_http_scheme_is_blocked_before_fetching(self) -> None:
        result = fetch_page_text("file:///C:/secret.txt", _policy(), 1000)

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

        policy = _policy()
        result = fetch_page_text("https://example.com/article", policy, 1000)

        self.assertIn("Public content", result.text)
        response.read.assert_called_once_with(policy.html_max_bytes + 1)
        build_opener.return_value.open.assert_called_once()

    @patch("feedian.extract.socket.getaddrinfo")
    def test_private_address_is_blocked(self, getaddrinfo) -> None:
        getaddrinfo.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]

        with self.assertRaisesRegex(ValueError, "non-public address"):
            validate_fetch_url("http://localhost:8080")

    @patch("feedian.extract.socket.getaddrinfo")
    def test_redirect_destination_is_checked(self, getaddrinfo) -> None:
        getaddrinfo.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", 80))]
        handler = SafeRedirectHandler(allowed_private_hosts=frozenset())

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
        handler = SafeRedirectHandler(allowed_private_hosts=frozenset())
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
            "https://example.com/new", allowed_private_hosts=frozenset()
        )
        self.assertEqual(redirected.full_url, "https://example.com/new")
        self.assertEqual(redirected.get_method(), "GET")

    def test_redirect_hop_gets_its_own_connection_instance(self) -> None:
        """Spec 20260820: each hop resolves/validates/connects independently.

        SafeRedirectHandler.redirect_request only validates and hands the
        opener a fresh Request; the actual per-hop pinning comes from every
        hop building a brand-new _Validating*Connection through do_open. There
        is no cross-hop state (like the removed browser cache) to leak.
        """
        handler = SafeRedirectHandler(allowed_private_hosts=frozenset())

        with patch("feedian.extract.validate_fetch_url") as validate:
            first = handler.redirect_request(
                Request("https://example.com/a"), None, 302, "Found", {}, "https://example.com/b"
            )
            second = handler.redirect_request(
                first, None, 302, "Found", {}, "https://example.com/c"
            )

        self.assertEqual(
            validate.call_args_list,
            [
                unittest.mock.call("https://example.com/b", allowed_private_hosts=frozenset()),
                unittest.mock.call("https://example.com/c", allowed_private_hosts=frozenset()),
            ],
        )
        self.assertEqual(second.full_url, "https://example.com/c")


class ValidatedCreateConnectionTests(unittest.TestCase):
    """Spec 20260820-fetch-config-integrity-hardening: DNS rebinding closed by
    pinning the connection to the exact addrinfo the SSRF check validated."""

    def test_validates_every_returned_address_and_rejects_if_any_is_private(self) -> None:
        with patch("feedian.extract.socket.getaddrinfo") as getaddrinfo:
            getaddrinfo.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
            ]
            with self.assertRaisesRegex(ValueError, "non-public address"):
                _validated_create_connection(
                    ("example.com", 443), 5, None, allowed_private_hosts=frozenset()
                )

    def test_connects_within_the_validated_address_set_when_all_are_public(self) -> None:
        with (
            patch("feedian.extract.socket.getaddrinfo") as getaddrinfo,
            patch("feedian.extract.socket.socket") as socket_ctor,
        ):
            getaddrinfo.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            ]
            sock = Mock()
            socket_ctor.return_value = sock

            result = _validated_create_connection(
                ("example.com", 443), 5, None, allowed_private_hosts=frozenset()
            )

        self.assertIs(result, sock)
        sock.connect.assert_called_once_with(("93.184.216.34", 443))

    def test_getaddrinfo_is_called_exactly_once_and_create_connection_is_never_used(self) -> None:
        with (
            patch("feedian.extract.socket.getaddrinfo") as getaddrinfo,
            patch("feedian.extract.socket.socket") as socket_ctor,
            patch("feedian.extract.socket.create_connection") as create_connection,
        ):
            getaddrinfo.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            ]
            socket_ctor.return_value = Mock()

            _validated_create_connection(("example.com", 443), 5, None, allowed_private_hosts=frozenset())

        getaddrinfo.assert_called_once_with("example.com", 443, 0, socket.SOCK_STREAM)
        create_connection.assert_not_called()

    def test_allowed_private_hosts_skips_the_check_for_that_host_only(self) -> None:
        with (
            patch("feedian.extract.socket.getaddrinfo") as getaddrinfo,
            patch("feedian.extract.socket.socket") as socket_ctor,
        ):
            getaddrinfo.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 8080)),
            ]
            sock = Mock()
            socket_ctor.return_value = sock

            result = _validated_create_connection(
                ("localhost", 8080), 5, None, allowed_private_hosts=frozenset({"localhost"})
            )

        self.assertIs(result, sock)

        with patch("feedian.extract.socket.getaddrinfo") as getaddrinfo:
            getaddrinfo.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", 80)),
            ]
            with self.assertRaisesRegex(ValueError, "non-public address"):
                _validated_create_connection(
                    ("192.168.1.1", 80), 5, None, allowed_private_hosts=frozenset({"localhost"})
                )

    def test_ipv6_sockaddr_is_passed_through_without_truncation(self) -> None:
        """A prior draft sliced sockaddr to (host, port), losing flowinfo/scope_id
        for IPv6. The corrected version connects with the sockaddr as-is."""
        ipv6_sockaddr = ("2001:db8::1", 443, 0, 0)
        with (
            patch("feedian.extract.socket.getaddrinfo") as getaddrinfo,
            patch("feedian.extract.socket.socket") as socket_ctor,
        ):
            getaddrinfo.return_value = [
                (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ipv6_sockaddr),
            ]
            sock = Mock()
            socket_ctor.return_value = sock

            _validated_create_connection(
                ("example.com", 443), 5, None, allowed_private_hosts=frozenset({"example.com"})
            )

        sock.connect.assert_called_once_with(ipv6_sockaddr)


class ValidatingConnectionClassTests(unittest.TestCase):
    """The connection classes swap _create_connection in __init__, not connect()."""

    def test_http_connection_swaps_create_connection(self) -> None:
        connection = _ValidatingHTTPConnection("example.com", allowed_private_hosts=frozenset({"a"}))

        self.assertNotEqual(connection._create_connection, socket.create_connection)
        self.assertEqual(connection._create_connection.keywords, {"allowed_private_hosts": frozenset({"a"})})

    def test_https_connection_swaps_create_connection(self) -> None:
        import ssl

        connection = _ValidatingHTTPSConnection(
            "example.com", allowed_private_hosts=frozenset({"a"}), context=ssl.create_default_context()
        )

        self.assertNotEqual(connection._create_connection, socket.create_connection)
        self.assertEqual(connection._create_connection.keywords, {"allowed_private_hosts": frozenset({"a"})})


class PlainHttpAndProxyTests(unittest.TestCase):
    """Spec 20260820: plain http:// goes through the validated connection too,
    and proxy env vars are not honored for Feedian's own fetch traffic."""

    @patch("feedian.extract.socket.getaddrinfo")
    def test_plain_http_request_uses_the_validating_http_handler(self, getaddrinfo) -> None:
        getaddrinfo.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]

        with patch("feedian.extract.build_opener") as build_opener:
            response = Mock()
            response.headers.get.return_value = "text/plain; charset=utf-8"
            response.read.return_value = b"hello"
            build_opener.return_value.open.return_value.__enter__.return_value = response

            fetch_page_text("http://example.com/article", _policy(), 1000)

        handlers = build_opener.call_args.args
        self.assertTrue(any(isinstance(handler, _ValidatingHTTPHandler) for handler in handlers))
        self.assertTrue(any(isinstance(handler, _ValidatingHTTPSHandler) for handler in handlers))

    @patch("feedian.extract.socket.getaddrinfo")
    def test_proxy_environment_variables_are_not_honored(self, getaddrinfo) -> None:
        getaddrinfo.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]

        with (
            patch.dict("os.environ", {"HTTP_PROXY": "http://proxy.invalid:8080", "HTTPS_PROXY": "http://proxy.invalid:8080"}),
            patch("feedian.extract.build_opener") as build_opener,
        ):
            response = Mock()
            response.headers.get.return_value = "text/plain; charset=utf-8"
            response.read.return_value = b"hello"
            build_opener.return_value.open.return_value.__enter__.return_value = response

            fetch_page_text("http://example.com/article", _policy(), 1000)

        from urllib.request import ProxyHandler

        handlers = build_opener.call_args.args
        proxy_handlers = [handler for handler in handlers if isinstance(handler, ProxyHandler)]
        self.assertEqual(len(proxy_handlers), 1)
        self.assertEqual(proxy_handlers[0].proxies, {})


class BrowserFallbackNetworkBoundaryTests(unittest.TestCase):
    """Spec 20260820 plus review 20260820-1: every browser transport is bounded.

    `_validated_browser_hosts` was a permanent cache that skipped revalidation
    for a host already seen. It is removed entirely, and the route now belongs
    to a Service-Worker-free context so popup requests are covered too.
    WebSockets use a separate Playwright route and are disabled.
    """

    def setUp(self) -> None:
        import feedian.extract as extract_module

        self._extract_module = extract_module
        self._original_browser = extract_module._browser
        self._original_runtime = extract_module._browser_runtime

    def tearDown(self) -> None:
        self._extract_module._browser = self._original_browser
        self._extract_module._browser_runtime = self._original_runtime

    def test_validate_fetch_url_runs_on_every_route_even_for_a_repeated_host(self) -> None:
        class FakeRequest:
            def __init__(self, url: str) -> None:
                self.url = url
                self.resource_type = "document"

        class FakeRoute:
            def __init__(self, request: FakeRequest) -> None:
                self.request = request

            def abort(self) -> None:
                pass

            def continue_(self) -> None:
                pass

        class FakeResponse:
            status = 200

        web_socket = Mock()

        class FakePage:
            def __init__(self, context) -> None:
                self._context = context

            def goto(self, target_url, wait_until, timeout):
                self._context.web_socket_handler(web_socket)
                # The same host requested twice in one render: a permanent
                # cache would only validate it once.
                self._context.route_handler(FakeRoute(FakeRequest(target_url)))
                self._context.route_handler(FakeRoute(FakeRequest(target_url)))
                return FakeResponse()

            def wait_for_timeout(self, _ms) -> None:
                pass

            @property
            def url(self) -> str:
                return "https://example.com/article"

            def content(self) -> str:
                return "<html></html>"

            def title(self) -> str:
                return "Title"

        class FakeContext:
            def __init__(self) -> None:
                self.route_handler = None
                self.web_socket_handler = None
                self.closed = False

            def route(self, _pattern, handler) -> None:
                self.route_handler = handler

            def route_web_socket(self, _pattern, handler) -> None:
                self.web_socket_handler = handler

            def new_page(self):
                return FakePage(self)

            def close(self) -> None:
                self.closed = True

        fake_browser = Mock()
        fake_context = FakeContext()
        fake_browser.new_context.return_value = fake_context
        self._extract_module._browser = fake_browser
        self._extract_module._browser_runtime = Mock()

        with patch("feedian.extract.validate_fetch_url") as validate:
            render_html_with_browser("https://example.com/article", policy=_policy())

        # Two identical in-page requests plus the final-URL check: three
        # separate validations, none of them skipped by a cache.
        self.assertEqual(validate.call_count, 3)
        fake_browser.new_context.assert_called_once_with(locale="ja-JP", service_workers="block")
        web_socket.close.assert_called_once_with(
            code=1008, reason="WebSockets are disabled during page extraction."
        )
        self.assertTrue(fake_context.closed)


class BrowserCandidateTests(unittest.TestCase):
    def test_browser_candidate_carries_the_full_fetch_policy(self) -> None:
        policy = _policy(browser_timeout_seconds=45)

        candidate = BrowserCandidate(kind="http-error", fetch_url="https://example.com/a", policy=policy)

        self.assertIs(candidate.policy, policy)
        self.assertEqual(candidate.policy.browser_timeout_seconds, 45)


if __name__ == "__main__":
    unittest.main()
