import unittest

from feedian.canonical import (
    canonical_item_from_metadata,
    canonicalize_url,
    source_id_for_url,
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


if __name__ == "__main__":
    unittest.main()
