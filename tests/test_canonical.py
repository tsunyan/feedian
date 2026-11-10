import unittest

from feedian.canonical import (
    canonical_item_from_metadata,
    canonicalize_url,
    source_id_for_url,
    strip_tracking_params,
    url_content_key,
)


class CanonicalTests(unittest.TestCase):
    def test_url_identity_ignores_fragment_and_default_port(self) -> None:
        first = "HTTPS://Example.COM:443/path?q=1#section"
        second = "https://example.com/path?q=1"

        self.assertEqual(canonicalize_url(first), second)
        self.assertEqual(url_content_key(first), url_content_key(second))
        self.assertEqual(source_id_for_url("hatena", first), source_id_for_url("hatena", second))

    def test_raindrop_metadata_can_be_canonicalized_for_shared_enrichments(self) -> None:
        item = canonical_item_from_metadata(
            {
                "_id": 123,
                "title": "Article",
                "link": "https://example.com/article",
                "excerpt": "Excerpt",
                "note": "Note",
                "tags": ["one"],
            }
        )

        self.assertEqual(item.source, "raindrop")
        self.assertEqual(item.source_id, "123")
        self.assertEqual(item.content_key, url_content_key(item.url))
        self.assertEqual(item.comment, "Note")

    def test_strip_tracking_params_removes_x_share_parameters(self) -> None:
        clean = "https://x.com/uhyo_/status/2117542695807909918"
        self.assertEqual(strip_tracking_params(clean + "?a=mc8n6VCR353PI-2SzPI&s=09"), clean)
        self.assertEqual(strip_tracking_params(clean + "?s=12"), clean)
        self.assertEqual(strip_tracking_params(clean + "?t=abc"), clean)
        self.assertEqual(
            strip_tracking_params("https://twitter.com/u/status/1?s=20"), "https://twitter.com/u/status/1"
        )
        self.assertEqual(
            strip_tracking_params("https://mobile.x.com/u/status/1?s=20"), "https://mobile.x.com/u/status/1"
        )
        self.assertEqual(strip_tracking_params("https://X.com/u/status/1?s=1"), "https://X.com/u/status/1")

    def test_strip_tracking_params_keeps_everything_else_verbatim(self) -> None:
        search = "https://x.com/search?q=%E3%81%BC%E3%81%8F&src=typed_query"
        self.assertEqual(strip_tracking_params(search), search)
        self.assertEqual(
            strip_tracking_params("https://x.com/search?q=x&s=20&f=live"), "https://x.com/search?q=x&f=live"
        )
        self.assertEqual(
            strip_tracking_params("https://x.com/u/status/1?s=12#frag"), "https://x.com/u/status/1#frag"
        )
        other = "https://example.com/?a=1&s=2"
        self.assertEqual(strip_tracking_params(other), other)

    def test_url_identity_ignores_x_share_parameters(self) -> None:
        dirty = "https://x.com/u/status/1?a=hash&s=09"
        clean = "https://x.com/u/status/1"
        self.assertEqual(canonicalize_url(dirty), canonicalize_url(clean))
        self.assertEqual(url_content_key(dirty), url_content_key(clean))
        self.assertEqual(source_id_for_url("hatena", dirty), source_id_for_url("hatena", clean))

    def test_raindrop_metadata_strips_x_share_parameters(self) -> None:
        item = canonical_item_from_metadata({"_id": 1, "link": "https://x.com/u/status/1?a=hash&s=09"})

        self.assertEqual(item.url, "https://x.com/u/status/1")
        self.assertEqual(item.provider_metadata, {})


if __name__ == "__main__":
    unittest.main()
