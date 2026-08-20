from __future__ import annotations

import warnings
from unittest.mock import patch
from urllib.error import HTTPError

from feedian.extract import _ValidatingHTTPHandler, _ValidatingHTTPSHandler
from feedian.rss import FEED_XML_MAX_BYTES, fetch_rss_items, parse_rss_items
from feedian.vault import NetworkPolicy


def test_parse_rss_items() -> None:
    items = parse_rss_items(
        b"""<rss><channel><item><guid>one</guid><title>First</title><link>https://example.test/one</link><description>&lt;b&gt;Summary&lt;/b&gt;</description><category>news</category><pubDate>Tue</pubDate></item></channel></rss>""",
        "https://example.test/feed.xml",
    )
    assert len(items) == 1
    assert items[0].item.source == "rss"
    assert items[0].item.url == "https://example.test/one"
    assert items[0].item.excerpt == "Summary"
    assert items[0].item.tags == ["news"]


def test_parse_rss_without_a_channel_element_falls_back_to_root_without_warning() -> None:
    """Spec 20260820-fetch-config-integrity-hardening: `or root` used to trigger a
    stdlib DeprecationWarning when Element.__bool__ was evaluated on an empty channel."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        items = parse_rss_items(
            b"""<rss><item><guid>one</guid><title>First</title><link>https://example.test/one</link></item></rss>""",
            "https://example.test/feed.xml",
        )
    assert len(items) == 1
    assert items[0].item.url == "https://example.test/one"


def test_parse_atom_items() -> None:
    items = parse_rss_items(
        b"""<feed xmlns='http://www.w3.org/2005/Atom'><entry><id>one</id><title>First</title><link href='https://example.test/one'/><summary>Summary</summary><category term='tech'/></entry></feed>""",
        "https://example.test/feed.xml",
    )
    assert len(items) == 1
    assert items[0].item.tags == ["tech"]


def test_parse_rss_10_namespaces_content_and_relative_link() -> None:
    items = parse_rss_items(
        b"""<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
              xmlns='http://purl.org/rss/1.0/' xmlns:dc='http://purl.org/dc/elements/1.1/'
              xmlns:content='http://purl.org/rss/1.0/modules/content/'>
          <channel><title>Example Feed</title><link>https://example.test/</link></channel>
          <item><title>Namespaced</title><link>/article</link><dc:date>2026-08-13T01:02:03Z</dc:date>
            <content:encoded>&lt;p&gt;Full body&lt;/p&gt;</content:encoded><category>AI</category>
          </item>
        </rdf:RDF>""",
        "https://example.test/feed.xml",
        feed_tags=["news"],
        category_routes={"AI": "technology/ai"},
    )

    assert len(items) == 1
    item = items[0].item
    assert item.url == "https://example.test/article"
    assert item.embedded_content == "Full body"
    assert item.tags == ["news", "AI"]
    assert item.provider_metadata["feed_title"] == "Example Feed"
    assert item.provider_metadata["feed_route"] == "technology/ai"
    assert item.provider_metadata["published_at"] == "2026-08-13T01:02:03Z"


def test_atom_prefers_html_alternate_and_uses_configured_folder() -> None:
    items = parse_rss_items(
        b"""<feed xmlns='http://www.w3.org/2005/Atom'><title>Remote Name</title><entry>
          <id>one</id><title>First</title>
          <link rel='alternate' type='application/json' href='/data.json'/>
          <link rel='alternate' type='text/html' href='/article'/>
          <content type='html'>&lt;p&gt;Article body&lt;/p&gt;</content>
          <published>2026-08-13T10:00:00+09:00</published>
        </entry></feed>""",
        "https://example.test/feed.xml",
        configured_name="Configured Name",
        feed_folder="Pinned Folder",
    )

    item = items[0].item
    assert item.url == "https://example.test/article"
    assert item.provider_metadata["feed_title"] == "Configured Name"
    assert item.provider_metadata["feed_folder"] == "Pinned Folder"
    assert item.embedded_content == "Article body"


def test_fetch_rss_uses_conditional_headers_and_accepts_not_modified(monkeypatch) -> None:
    captured = {}

    class Opener:
        def open(self, request, timeout):
            captured["etag"] = request.get_header("If-none-match")
            captured["modified"] = request.get_header("If-modified-since")
            raise HTTPError(request.full_url, 304, "Not Modified", {}, None)

    monkeypatch.setattr("feedian.rss.validate_fetch_url", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("feedian.rss.build_opener", lambda *_args, **_kwargs: Opener())

    items = fetch_rss_items(
        "https://example.test/feed.xml",
        etag='"feed-version"',
        last_modified="Thu, 13 Aug 2026 00:00:00 GMT",
    )

    assert items == []
    assert captured == {
        "etag": '"feed-version"',
        "modified": "Thu, 13 Aug 2026 00:00:00 GMT",
    }


def test_fetch_rss_uses_the_validating_handlers_with_its_own_network_policy() -> None:
    """Spec 20260820: RSS shares NetworkPolicy (the SSRF/proxy transport) with
    page fetch, but keeps its own timeout_seconds default and FEED_XML_MAX_BYTES
    rather than taking a whole FetchPolicy."""
    network = NetworkPolicy(allowed_private_hosts=frozenset({"internal.example.test"}))

    with (
        patch("feedian.rss.validate_fetch_url") as validate,
        patch("feedian.rss.build_opener") as build_opener,
    ):
        opener = build_opener.return_value
        opener.open.return_value.__enter__.return_value = _fake_response(b"<rss><channel></channel></rss>")

        fetch_rss_items("https://example.test/feed.xml", network=network)

    validate.assert_called_once_with(
        "https://example.test/feed.xml", allowed_private_hosts=network.allowed_private_hosts
    )
    handlers = build_opener.call_args.args
    http_handlers = [h for h in handlers if isinstance(h, _ValidatingHTTPHandler)]
    https_handlers = [h for h in handlers if isinstance(h, _ValidatingHTTPSHandler)]
    assert len(http_handlers) == 1
    assert len(https_handlers) == 1
    assert http_handlers[0]._allowed_private_hosts == network.allowed_private_hosts
    assert https_handlers[0]._allowed_private_hosts == network.allowed_private_hosts


def test_fetch_rss_default_timeout_and_xml_limit_are_unchanged_by_network_policy() -> None:
    import inspect

    signature = inspect.signature(fetch_rss_items)
    assert signature.parameters["timeout_seconds"].default == 30
    assert FEED_XML_MAX_BYTES == 10 * 1024 * 1024


def _fake_response(body: bytes):
    class Response:
        headers = {"ETag": "", "Last-Modified": ""}

        def read(self, _limit):
            return body

        def geturl(self):
            return "https://example.test/feed.xml"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    return Response()
