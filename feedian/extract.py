from __future__ import annotations

import atexit
from io import BytesIO
import ipaddress
import re
import socket
import ssl
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener

from charset_normalizer import from_bytes
from lxml import html as lxml_html
from pypdf import PdfReader
from trafilatura import extract as extract_main_text
from trafilatura import extract_metadata, html2txt
from w3lib.encoding import html_to_unicode


MAX_HTML_BYTES = 10 * 1024 * 1024
MAX_DOCUMENT_BYTES = 100 * 1024 * 1024
MIN_HIGH_CONFIDENCE_CHARS = 200


@dataclass
class PageFetchResult:
    url: str
    text: str
    title: str = ""
    error: str | None = None
    fetch_method: str = ""
    extraction_method: str = ""
    content_encoding: str = ""
    content_truncated: bool = False
    discussion_text: str = ""
    final_url: str = ""
    media_type: str = ""
    response_headers: dict[str, str] | None = None
    http_status: int | None = None
    raw_body: bytes | None = None
    rendered_html: str = ""
    payload_too_large: bool = False
    not_modified: bool = False
    failure_kind: str | None = None
    browser_pending: "BrowserCandidate | None" = None


@dataclass(frozen=True)
class _HtmlFetch:
    """The HTTP side of one HTML fetch, kept whole so the merge can be redone.

    A deferred browser render has to be compared against exactly what the HTTP
    response produced, including the payload, status and headers that
    should_fetch_resource later reads.
    """

    url: str
    decoded: str
    final_url: str
    encoding: str
    truncated: bool
    content_type: str
    response_headers: dict[str, str] | None
    status: int | None
    raw: bytes | None
    parts: "ExtractedPageParts"


@dataclass(frozen=True)
class BrowserCandidate:
    """A browser render a worker thread could not run itself."""

    kind: str
    fetch_url: str
    allow_private_urls: bool
    timeout_seconds: int
    initial_error: str = ""
    fetch: _HtmlFetch | None = None


@dataclass
class ExtractedPageParts:
    text: str
    title: str
    method: str
    discussion_text: str = ""


@dataclass(frozen=True)
class ContentImage:
    url: str
    alt_text: str = ""


@dataclass(eq=False)
class TextCandidate:
    priority: int
    parts: list[str]


class UnresolvableHostError(ValueError):
    """The hostname does not resolve. Distinct from the SSRF guard's rejection."""


class SafeRedirectHandler(HTTPRedirectHandler):
    def __init__(self, allow_private_urls: bool) -> None:
        super().__init__()
        self.allow_private_urls = allow_private_urls

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        destination = urljoin(req.full_url, newurl)
        validate_fetch_url(destination, allow_private_urls=self.allow_private_urls)
        # Python 3.12's urllib follows 307 but does not recognize 308. Both
        # preserve the request method, so treat 308 as 307 after validating
        # the destination. Feedian only issues GET/HEAD content requests.
        redirect_code = 307 if code == 308 else code
        return super().redirect_request(req, fp, redirect_code, msg, headers, destination)

    # urllib does not dispatch 308 responses to HTTPRedirectHandler on the
    # supported Python version. Reuse its loop detection and Location parsing.
    http_error_308 = HTTPRedirectHandler.http_error_302


class TextExtractor(HTMLParser):
    block_tags = {
        "article", "blockquote", "br", "dd", "div", "dt", "figcaption", "h1", "h2", "h3",
        "h4", "h5", "h6", "li", "main", "p", "pre", "section", "td", "th", "tr",
    }
    skip_tags = {"canvas", "dialog", "form", "noscript", "script", "style", "svg", "template"}
    excluded_sections = {"aside", "footer", "header", "nav"}
    excluded_keywords = {
        "ad", "ads", "advert", "advertisement", "banner", "comment", "comments", "consent",
        "cookie", "footer", "header", "nav", "navigation", "promo", "recommend", "related",
        "reply", "share", "sidebar", "social", "sponsored",
    }
    candidate_keywords = {"article", "content", "entry", "main", "post", "story"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self.candidates: list[TextCandidate] = []
        self._active_candidates: list[TextCandidate] = []
        self._candidate_stack: list[TextCandidate | None] = []
        self._excluded_stack: list[bool] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = True

        excluded = tag in self.skip_tags or tag in self.excluded_sections or self._is_excluded(attrs)
        self._excluded_stack.append(excluded)
        if excluded:
            self._skip_depth += 1
            self._candidate_stack.append(None)
            return
        if self._skip_depth:
            self._candidate_stack.append(None)
            return

        candidate = self._candidate_for(tag, attrs)
        self._candidate_stack.append(candidate)
        if candidate is not None:
            self.candidates.append(candidate)
            self._active_candidates.append(candidate)
        if tag in self.block_tags:
            self._append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = False

        candidate = self._candidate_stack.pop() if self._candidate_stack else None
        excluded = self._excluded_stack.pop() if self._excluded_stack else False
        if candidate is not None:
            self._active_candidates.remove(candidate)
        if excluded:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif self._skip_depth == 0 and tag in self.block_tags:
            self._append("\n")

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self.title_parts.append(text)
        if not self._skip_depth and not self._in_title:
            self._append(text)

    def _append(self, text: str) -> None:
        self.parts.append(text)
        for candidate in self._active_candidates:
            candidate.parts.append(text)

    def _candidate_for(self, tag: str, attrs: list[tuple[str, str | None]]) -> TextCandidate | None:
        if tag == "article":
            return TextCandidate(priority=3, parts=[])
        if tag == "main":
            return TextCandidate(priority=2, parts=[])
        if self._has_candidate_keyword(attrs):
            return TextCandidate(priority=1, parts=[])
        return None

    def _is_excluded(self, attrs: list[tuple[str, str | None]]) -> bool:
        attributes = {name.lower(): (value or "").lower() for name, value in attrs}
        if attributes.get("role") in {"banner", "complementary", "contentinfo", "navigation"}:
            return True
        tokens = self._attribute_tokens(attributes)
        return bool(tokens & self.excluded_keywords)

    def _has_candidate_keyword(self, attrs: list[tuple[str, str | None]]) -> bool:
        attributes = {name.lower(): (value or "").lower() for name, value in attrs}
        return bool(self._attribute_tokens(attributes) & self.candidate_keywords)

    @staticmethod
    def _attribute_tokens(attributes: dict[str, str]) -> set[str]:
        values = " ".join(attributes.get(name, "") for name in ("class", "id", "role"))
        return set(re.findall(r"[a-z0-9]+", values.lower()))

    @property
    def text(self) -> str:
        candidates = [
            (candidate.priority, normalize_text(candidate.parts))
            for candidate in self.candidates
            if normalize_text(candidate.parts)
        ]
        if candidates:
            best_priority = max(priority for priority, _ in candidates)
            return max((text for priority, text in candidates if priority == best_priority), key=len)
        return normalize_text(self.parts)

    @property
    def title(self) -> str:
        return normalize_inline(" ".join(self.title_parts))


def fetch_page_text(
    url: str,
    timeout_seconds: int,
    max_chars: int,
    allow_private_urls: bool = False,
    etag: str = "",
    last_modified: str = "",
    browser_timeout_seconds: int = 30,
    allow_browser: bool = True,
) -> PageFetchResult:
    # max_chars remains in the public call signature for compatibility. Extracted
    # text is stored in full; the limit is applied only when building an LLM prompt.
    _ = max_chars
    fetch_url = resolve_content_url(url)
    try:
        validate_fetch_url(fetch_url, allow_private_urls=allow_private_urls)
    except ValueError as exc:
        failure_kind = "dns" if isinstance(exc, UnresolvableHostError) else None
        return PageFetchResult(url=url, text="", error=f"blocked URL: {exc}", failure_kind=failure_kind)

    headers = {
        "User-Agent": "feedian/0.1 (+https://github.com/) Python urllib",
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1",
    }
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    request = Request(
        fetch_url,
        headers=headers,
        method="GET",
    )
    try:
        context = ssl.create_default_context()
        opener = build_opener(HTTPSHandler(context=context), SafeRedirectHandler(allow_private_urls))
        with opener.open(request, timeout=timeout_seconds) as response:
            content_type = response.headers.get("Content-Type", "")
            is_html = "text/html" in content_type or "application/xhtml" in content_type
            limit = MAX_HTML_BYTES if is_html else MAX_DOCUMENT_BYTES
            raw = response.read(limit + 1)
            try:
                response_headers = {str(key): str(value) for key, value in response.headers.items()}
            except (AttributeError, TypeError, ValueError):
                response_headers = {}
            status = getattr(response, "status", None)
            geturl = getattr(response, "geturl", None)
            response_url = geturl() if callable(geturl) else None
            final_url = response_url if isinstance(response_url, str) else fetch_url
    except HTTPError as exc:
        if exc.code == 304:
            response_headers = {str(key): str(value) for key, value in exc.headers.items()} if exc.headers else {}
            return PageFetchResult(
                url=url,
                text="",
                final_url=fetch_url,
                response_headers=response_headers,
                http_status=304,
                fetch_method="http",
                not_modified=True,
            )
        if exc.code in {401, 403, 406}:
            # No HTTP body exists to compare against here, so the browser result
            # stands alone. Deferring it keeps playwright on one thread.
            pending = BrowserCandidate(
                kind="http-error",
                fetch_url=fetch_url,
                allow_private_urls=allow_private_urls,
                timeout_seconds=browser_timeout_seconds,
                initial_error=f"HTTP {exc.code}",
            )
            if not allow_browser:
                return PageFetchResult(
                    url=url, text="", http_status=exc.code, browser_pending=pending
                )
            return complete_browser_fallback(
                PageFetchResult(url=url, text="", http_status=exc.code, browser_pending=pending)
            )
        return PageFetchResult(url=url, text="", error=f"HTTP {exc.code}", http_status=exc.code)
    except URLError as exc:
        failure_kind = "timeout" if isinstance(exc.reason, TimeoutError) else None
        return PageFetchResult(url=url, text="", error=str(exc.reason), failure_kind=failure_kind)
    except Exception as exc:
        failure_kind = "timeout" if isinstance(exc, TimeoutError) else None
        return PageFetchResult(url=url, text="", error=str(exc), failure_kind=failure_kind)

    too_large = len(raw) > limit
    if too_large and not is_html:
        return PageFetchResult(
            url=url,
            text="",
            error=f"document exceeds {MAX_DOCUMENT_BYTES} byte safety limit",
            final_url=final_url,
            media_type=content_type,
            response_headers=response_headers,
            http_status=status if isinstance(status, int) else None,
            payload_too_large=True,
        )
    truncated = too_large
    if is_html:
        raw = raw[:MAX_HTML_BYTES]
    if "application/pdf" in content_type.lower():
        result = extract_stored_payload(raw, final_url, content_type)
        result.url = url
        result.fetch_method = "http"
        result.response_headers = response_headers
        result.http_status = status if isinstance(status, int) else None
        return result
    if "text/html" not in content_type and "text/plain" not in content_type and "application/xhtml" not in content_type:
        return PageFetchResult(
            url=url,
            text="",
            error=f"unsupported content type: {content_type or 'unknown'}",
            final_url=final_url,
            media_type=content_type,
            response_headers=response_headers,
            http_status=status if isinstance(status, int) else None,
            raw_body=raw,
            content_truncated=truncated,
        )

    encoding, decoded = decode_html(raw, content_type)
    if "text/plain" in content_type:
        text = normalize_plain_text(decoded)
        return PageFetchResult(
            url=url,
            text=text,
            fetch_method="http",
            extraction_method="plain-text",
            content_encoding=encoding,
            content_truncated=truncated,
            error="HTML download truncated at 10 MiB" if truncated else None,
            final_url=final_url,
            media_type=content_type,
            response_headers=response_headers,
            http_status=status if isinstance(status, int) else None,
            raw_body=raw,
        )

    http_parts = extract_page_parts(decoded, final_url)
    fetch = _HtmlFetch(
        url=url,
        decoded=decoded,
        final_url=final_url,
        encoding=encoding,
        truncated=truncated,
        content_type=content_type,
        response_headers=response_headers,
        status=status if isinstance(status, int) else None,
        raw=raw,
        parts=http_parts,
    )
    if not should_render_with_browser(http_parts.text, decoded, http_parts.title):
        return _merge_html_result(fetch)
    if not allow_browser:
        # playwright's sync API is bound to the thread that started it, so a
        # worker hands the render back rather than running it. The tail is left
        # unfinished on purpose: running the html2txt fallback now would change
        # what the browser text is later compared against.
        return _merge_html_result(
            fetch,
            pending=BrowserCandidate(
                kind="low-quality",
                fetch_url=fetch_url,
                allow_private_urls=allow_private_urls,
                timeout_seconds=browser_timeout_seconds,
                fetch=fetch,
            ),
        )
    rendered, browser_error = _render_for_merge(
        fetch_url, timeout_seconds=browser_timeout_seconds, allow_private_urls=allow_private_urls
    )
    return _merge_html_result(fetch, rendered=rendered, browser_error=browser_error)


def _render_for_merge(
    fetch_url: str, *, timeout_seconds: int, allow_private_urls: bool
) -> tuple[tuple[str, str, str] | None, str | None]:
    """Render one page, reporting failure instead of raising.

    A browser that fails must not cost us the HTTP body we already hold; the
    caller keeps that body and only appends the reason to the warning.
    """
    try:
        return (
            render_html_with_browser(
                fetch_url, timeout_seconds=timeout_seconds, allow_private_urls=allow_private_urls
            ),
            None,
        )
    except Exception as exc:
        return None, str(exc)


def _merge_html_result(
    fetch: _HtmlFetch,
    *,
    rendered: tuple[str, str, str] | None = None,
    browser_error: str | None = None,
    pending: BrowserCandidate | None = None,
) -> PageFetchResult:
    """Choose between the HTTP and browser extractions and build the result.

    The single definition of best-of-two. fetch_page_text calls it directly when
    it may drive the browser itself, and the main thread calls it through
    complete_browser_fallback when a worker deferred the render. Two copies of
    this rule would drift, and the HTTP payload, status and headers it carries
    forward are what the retry-suppression rules read.
    """
    best_text = fetch.parts.text
    best_title = fetch.parts.title
    best_method = fetch.parts.method
    best_discussion = fetch.parts.discussion_text
    best_html = fetch.decoded
    fetch_method = "http"
    if rendered is not None:
        rendered_html, rendered_url, rendered_title = rendered
        browser_parts = extract_page_parts(rendered_html, rendered_url)
        if text_quality_score(browser_parts.text) > text_quality_score(best_text):
            best_text = browser_parts.text
            best_title = browser_parts.title or rendered_title
            best_method = browser_parts.method
            best_discussion = browser_parts.discussion_text
            best_html = rendered_html
            fetch_method = "browser"

    if not best_text:
        all_text = normalize_plain_text(html2txt(best_html) or "")
        if text_quality_score(all_text) > text_quality_score(best_text):
            best_text = all_text
            best_method = "trafilatura-html2txt"

    warning: str | None = None
    if fetch.truncated:
        warning = "HTML download truncated at 10 MiB"
    if not best_text:
        warning = "no extractable text found"
        if browser_error:
            warning += f"; browser fallback failed: {browser_error}"
    elif not is_high_confidence_text(best_text):
        warning = f"low-confidence extraction: {len(best_text)} characters"
        if browser_error:
            warning += f"; browser fallback failed: {browser_error}"

    return PageFetchResult(
        url=fetch.url,
        text=best_text,
        title=best_title,
        error=warning,
        fetch_method=fetch_method,
        extraction_method=best_method,
        content_encoding=fetch.encoding,
        content_truncated=fetch.truncated,
        discussion_text=best_discussion,
        final_url=fetch.final_url,
        media_type=fetch.content_type,
        response_headers=fetch.response_headers,
        http_status=fetch.status,
        raw_body=fetch.raw,
        rendered_html=best_html if fetch_method == "browser" else "",
        browser_pending=pending,
    )


def complete_browser_fallback(page: PageFetchResult) -> PageFetchResult:
    """Run a deferred browser render on the calling thread and merge it in.

    Call this only from the thread that owns the playwright runtime. A result
    with nothing pending is returned unchanged, so callers need no branch.
    """
    pending = page.browser_pending
    if pending is None:
        return page
    if pending.kind == "http-error":
        try:
            return fetch_page_text_with_browser(
                original_url=page.url,
                fetch_url=pending.fetch_url,
                timeout_seconds=pending.timeout_seconds,
                allow_private_urls=pending.allow_private_urls,
                initial_error=pending.initial_error,
            )
        except Exception as exc:
            return PageFetchResult(
                url=page.url,
                text="",
                error=f"{pending.initial_error}; browser fallback failed: {exc}",
                http_status=page.http_status,
            )
    if pending.kind != "low-quality" or pending.fetch is None:
        # Not an assert: python -O strips those, and the merge would then read
        # attributes off None instead of saying what went wrong.
        raise ValueError(f"Unsupported browser candidate: {pending.kind}")
    rendered, browser_error = _render_for_merge(
        pending.fetch_url,
        timeout_seconds=pending.timeout_seconds,
        allow_private_urls=pending.allow_private_urls,
    )
    return _merge_html_result(pending.fetch, rendered=rendered, browser_error=browser_error)


def fetch_page_text_with_browser(
    *,
    original_url: str,
    fetch_url: str,
    timeout_seconds: int,
    allow_private_urls: bool,
    initial_error: str,
) -> PageFetchResult:
    rendered_html, rendered_url, rendered_title = render_html_with_browser(
        fetch_url,
        timeout_seconds=timeout_seconds,
        allow_private_urls=allow_private_urls,
    )
    parts = extract_page_parts(rendered_html, rendered_url)
    text, title, method = parts.text, parts.title, parts.method
    if not text:
        all_text = normalize_plain_text(html2txt(rendered_html) or "")
        if text_quality_score(all_text) > text_quality_score(text):
            text = all_text
            method = "trafilatura-html2txt"
    warning = None
    if not text:
        warning = f"{initial_error}; no extractable text found after browser fallback"
    elif not is_high_confidence_text(text):
        warning = f"{initial_error}; low-confidence browser extraction: {len(text)} characters"
    return PageFetchResult(
        url=original_url,
        text=text,
        title=title or rendered_title,
        error=warning,
        fetch_method="browser",
        extraction_method=method,
        content_encoding="browser",
        discussion_text=parts.discussion_text,
        final_url=rendered_url,
        media_type="text/html; charset=utf-8",
        rendered_html=rendered_html,
    )


def decode_html(raw: bytes, content_type: str | None) -> tuple[str, str]:
    return html_to_unicode(
        content_type,
        raw,
        default_encoding="utf-8",
        auto_detect_fun=_detect_encoding,
    )


def extract_stored_payload(raw: bytes, url: str, media_type: str) -> PageFetchResult:
    """Re-run deterministic extraction from bytes already preserved in SQLite."""
    normalized_type = media_type.lower()
    if "application/pdf" in normalized_type:
        try:
            reader = PdfReader(BytesIO(raw))
            pages = [normalize_plain_text(page.extract_text() or "") for page in reader.pages]
            text = "\n\n---\n\n".join(page for page in pages if page)
            title = str((reader.metadata.title if reader.metadata else "") or "").strip()
            warning = None if text else "PDF contains no extractable text; OCR may be required"
        except Exception as exc:
            text = ""
            title = ""
            warning = f"PDF text extraction failed: {exc}"
        return PageFetchResult(
            url=url, final_url=url, text=text, title=title, error=warning,
            fetch_method="stored", extraction_method="pypdf", media_type=media_type, raw_body=raw,
        )
    if "text/html" in normalized_type or "application/xhtml" in normalized_type:
        encoding, decoded = decode_html(raw, media_type)
        parts = extract_page_parts(decoded, url)
        warning = None if parts.text else "no extractable text found"
        return PageFetchResult(
            url=url, final_url=url, text=parts.text, title=parts.title, error=warning,
            fetch_method="stored", extraction_method=parts.method, content_encoding=encoding,
            discussion_text=parts.discussion_text, media_type=media_type, raw_body=raw,
        )
    if "text/plain" in normalized_type:
        encoding, decoded = decode_html(raw, media_type)
        return PageFetchResult(
            url=url, final_url=url, text=normalize_plain_text(decoded), fetch_method="stored",
            extraction_method="plain-text", content_encoding=encoding, media_type=media_type, raw_body=raw,
        )
    return PageFetchResult(
        url=url, final_url=url, text="", error=f"unsupported content type: {media_type or 'unknown'}",
        fetch_method="stored", media_type=media_type, raw_body=raw,
    )


def _detect_encoding(raw: bytes) -> str | None:
    match = from_bytes(raw).best()
    return match.encoding if match is not None else None


def extract_html(html: str, url: str) -> tuple[str, str, str]:
    parts = extract_page_parts(html, url)
    return parts.text, parts.title, parts.method


def extract_page_parts(html: str, url: str) -> ExtractedPageParts:
    if (urlparse(url).hostname or "").lower() == "anond.hatelabo.jp":
        specialized = extract_anond_parts(html, url)
        if specialized.text:
            return specialized
    precision_text = extract_main_text(
        html,
        url=url,
        output_format="markdown",
        include_comments=False,
        include_tables=True,
        include_formatting=True,
        include_links=True,
        favor_precision=True,
        deduplicate=False,
    )
    recall_text = extract_main_text(
        html,
        url=url,
        output_format="markdown",
        include_comments=False,
        include_tables=True,
        include_formatting=True,
        include_links=True,
        favor_recall=True,
        deduplicate=False,
    )
    metadata = extract_metadata(html, default_url=url)
    title = str(metadata.title or "").strip() if metadata is not None else ""
    precision = clean_extracted_text(normalize_plain_text(precision_text or ""), url)
    recall = clean_extracted_text(normalize_plain_text(recall_text or ""), url)
    if should_use_recall_fallback(precision, recall):
        return ExtractedPageParts(recall, title, "trafilatura-recall-fallback")
    return ExtractedPageParts(precision, title, "trafilatura")


def extract_content_images(html: str, url: str) -> list[ContentImage]:
    """Return distinct, likely editorial images without downloading anything.

    The selector is intentionally conservative: article/main content wins, then
    a conventional content container, and only then the document body. Ads,
    trackers, decorative pixels, and data URIs are excluded before storage.
    """
    try:
        document = lxml_html.fromstring(html)
    except (TypeError, ValueError):
        return []
    candidates = document.xpath("//article | //main")
    if not candidates:
        candidates = document.xpath(
            "//*[contains(concat(' ', normalize-space(@class), ' '), ' content ') "
            "or contains(concat(' ', normalize-space(@class), ' '), ' article ') "
            "or contains(concat(' ', normalize-space(@class), ' '), ' entry ') "
            "or contains(concat(' ', normalize-space(@class), ' '), ' post ')]"
        )
    container = max(candidates, key=lambda node: len(node.text_content() or ""), default=document)
    images: list[ContentImage] = []
    seen: set[str] = set()
    for image in container.xpath(".//img"):
        if _is_non_content_image(image):
            continue
        source = _image_source(image)
        if not source or source.startswith(("data:", "blob:")):
            continue
        absolute = urljoin(url, source)
        if absolute in seen:
            continue
        seen.add(absolute)
        images.append(ContentImage(absolute, " ".join(image.get("alt", "").split())))
    return images


def _image_source(image) -> str:
    for name in ("src", "data-src", "data-original", "data-lazy-src"):
        value = image.get(name)
        if value:
            return value.strip()
    for name in ("srcset", "data-srcset"):
        srcset = image.get(name)
        if srcset:
            # The last srcset candidate conventionally has the greatest width.
            return srcset.split(",")[-1].strip().split()[0]
    return ""


def _is_non_content_image(image) -> bool:
    attributes = " ".join(
        value or "" for value in (image.get("class"), image.get("id"), image.get("role"), image.get("aria-label"))
    ).lower()
    if re.search(r"(?:^|[-_\s])(ad|ads|advert|advertisement|banner|promo|sponsor|tracking|pixel)(?:$|[-_\s])", attributes):
        return True
    try:
        width = int(image.get("width", "0"))
        height = int(image.get("height", "0"))
    except ValueError:
        return False
    return 0 < width <= 10 and 0 < height <= 10


def clean_extracted_text(text: str, url: str) -> str:
    """Remove narrowly identified publisher boilerplate from extracted Markdown."""
    text = re.sub(r"(?im)^[ \t]*Advertisement[ \t]*$", "", text)
    hostname = (urlparse(url).hostname or "").lower()
    if hostname == "47news.jp" or hostname.endswith(".47news.jp"):
        text = re.sub(
            r"(?ims)\n?^(?:#{1,6}[ \t]*)?ピックアップ求人情報[ \t]*$.*\Z",
            "",
            text,
        )
    return normalize_plain_text(text)


def extract_anond_parts(html: str, url: str) -> ExtractedPageParts:
    try:
        document = lxml_html.fromstring(html)
    except (TypeError, ValueError):
        return ExtractedPageParts("", "", "trafilatura")
    body_nodes = document.xpath(
        "//div[contains(concat(' ', normalize-space(@class), ' '), ' day ')]"
        "/div[contains(concat(' ', normalize-space(@class), ' '), ' body ')]"
    )
    discussion_nodes = document.xpath(
        "//div[contains(concat(' ', normalize-space(@class), ' '), ' day ')]"
        "/div[contains(concat(' ', normalize-space(@class), ' '), ' refererlist ')]"
    )
    if not body_nodes:
        return ExtractedPageParts("", "", "trafilatura")
    body = body_nodes[0]
    for unwanted in body.xpath(
        ".//script | .//style | .//*[contains(concat(' ', normalize-space(@class), ' '), ' ad-in-entry-block ')]"
        " | .//*[contains(concat(' ', normalize-space(@class), ' '), ' sectionfooter ')]"
        " | .//*[contains(concat(' ', normalize-space(@class), ' '), ' share-button ')]"
    ):
        unwanted.drop_tree()
    text = normalize_plain_text(body.text_content())
    discussion = ""
    if discussion_nodes:
        discussion_html = lxml_html.tostring(discussion_nodes[0], encoding="unicode")
        discussion = normalize_plain_text(
            extract_main_text(
                discussion_html,
                url=url,
                output_format="markdown",
                include_comments=True,
                include_tables=True,
                include_formatting=True,
                include_links=True,
                favor_recall=True,
                deduplicate=False,
            )
            or discussion_nodes[0].text_content()
        )
    metadata = extract_metadata(html, default_url=url)
    title = str(metadata.title or "").strip() if metadata is not None else ""
    return ExtractedPageParts(text, title, "anond-dom", discussion)


def should_render_with_browser(text: str, html: str, title: str = "") -> bool:
    compact_length = len(re.sub(r"\s+", "", text))
    if not text:
        return True
    if text.count("\ufffd") / max(1, len(text)) >= 0.001:
        return True
    normalized_title = re.sub(r"\s+", "", title)
    normalized_text = re.sub(r"\s+", "", text)
    if compact_length >= 40 and normalized_title and normalized_title in normalized_text:
        return False
    if compact_length < 80:
        return True
    if compact_length < MIN_HIGH_CONFIDENCE_CHARS and re.search(
        r"<(?:div|main)[^>]+id=[\"'](?:app|root|__next|__nuxt)[\"']",
        html,
        re.IGNORECASE,
    ):
        return True
    return False


def should_use_recall_fallback(precision: str, recall: str) -> bool:
    precision_length = len(re.sub(r"\s+", "", precision))
    recall_length = len(re.sub(r"\s+", "", recall))
    if not precision:
        return bool(recall)
    return precision_length < 120 and recall_length >= 500 and recall_length >= precision_length * 3


def is_high_confidence_text(text: str) -> bool:
    compact_length = len(re.sub(r"\s+", "", text))
    if compact_length < MIN_HIGH_CONFIDENCE_CHARS:
        return False
    return text.count("\ufffd") / max(1, len(text)) < 0.001


def text_quality_score(text: str) -> tuple[int, int, int]:
    compact_length = len(re.sub(r"\s+", "", text))
    replacement_count = text.count("\ufffd")
    return (int(is_high_confidence_text(text)), -replacement_count, compact_length)


def resolve_content_url(url: str) -> str:
    parsed = urlparse(url)
    if (parsed.hostname or "").lower() == "megalodon.jp" and not parsed.path.startswith("/ref/"):
        return urlunparse(parsed._replace(path=f"/ref{parsed.path}"))
    return url


_browser_runtime = None
_browser = None
_validated_browser_hosts: set[tuple[str, bool]] = set()


def render_html_with_browser(
    url: str,
    *,
    timeout_seconds: int,
    allow_private_urls: bool,
) -> tuple[str, str, str]:
    global _browser_runtime, _browser
    if _browser is None:
        from playwright.sync_api import sync_playwright

        _browser_runtime = sync_playwright().start()
        _browser = _browser_runtime.chromium.launch(headless=True)
    page = _browser.new_page(locale="ja-JP")

    def route_request(route) -> None:
        request = route.request
        if request.resource_type in {"image", "media", "font", "stylesheet"}:
            route.abort()
            return
        parsed = urlparse(request.url)
        if parsed.scheme in {"data", "blob", "about"}:
            route.continue_()
            return
        host_key = (f"{parsed.scheme}://{parsed.hostname}:{parsed.port or ''}", allow_private_urls)
        try:
            if host_key not in _validated_browser_hosts:
                validate_fetch_url(request.url, allow_private_urls=allow_private_urls)
                _validated_browser_hosts.add(host_key)
        except ValueError:
            route.abort()
            return
        route.continue_()

    page.route("**/*", route_request)
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_seconds * 1000)
        if response is not None and response.status >= 400:
            raise RuntimeError(f"browser HTTP {response.status}")
        page.wait_for_timeout(1500)
        final_url = page.url
        validate_fetch_url(final_url, allow_private_urls=allow_private_urls)
        return page.content(), final_url, page.title()
    finally:
        page.close()


def close_browser() -> None:
    global _browser_runtime, _browser
    if _browser is not None:
        _browser.close()
        _browser = None
    if _browser_runtime is not None:
        _browser_runtime.stop()
        _browser_runtime = None


atexit.register(close_browser)


def validate_fetch_url(url: str, allow_private_urls: bool = False) -> None:
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("only http and https URLs are allowed")
    if not parsed.hostname:
        raise ValueError("URL does not include a hostname")
    if allow_private_urls:
        return

    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnresolvableHostError(f"hostname could not be resolved: {parsed.hostname}") from exc
    for _, _, _, _, sockaddr in addresses:
        address = ipaddress.ip_address(sockaddr[0].split("%", 1)[0])
        if not address.is_global:
            raise ValueError(f"non-public address is not allowed: {address}")


def charset_from_content_type(content_type: str) -> str | None:
    match = re.search(r"charset=([^;\s]+)", content_type, re.IGNORECASE)
    if not match:
        return None
    return match.group(1).strip("\"'")


def normalize_text(parts: Iterable[str]) -> str:
    text = unescape(" ".join(parts))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n+ *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_plain_text(text: str) -> str:
    text = unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_inline(text: str) -> str:
    return re.sub(r"\s+", " ", unescape(text)).strip()
