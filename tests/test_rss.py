from __future__ import annotations

from feedian.rss import parse_rss_items


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
