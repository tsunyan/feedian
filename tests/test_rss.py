from __future__ import annotations

from urllib.error import HTTPError

from feedian.rss import fetch_rss_items, parse_rss_items


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
