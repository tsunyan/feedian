import unittest

from raindian.extract import TextExtractor


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


if __name__ == "__main__":
    unittest.main()
