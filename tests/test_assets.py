from __future__ import annotations

from feedian.extract import extract_content_images


def test_extract_content_images_uses_article_and_ignores_ads_and_pixels() -> None:
    html = """
    <html><body>
      <img src="/outside.png">
      <article>
        <img src="hero.jpg" alt=" Hero image ">
        <img data-src="lazy.png" alt="lazy">
        <img src="ad.png" class="advertisement">
        <img src="pixel.gif" width="1" height="1">
        <img src="data:image/png;base64,abc">
      </article>
    </body></html>
    """

    images = extract_content_images(html, "https://example.test/posts/1")

    assert [(image.url, image.alt_text) for image in images] == [
        ("https://example.test/posts/hero.jpg", "Hero image"),
        ("https://example.test/posts/lazy.png", "lazy"),
    ]
