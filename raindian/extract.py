from __future__ import annotations

import re
import ssl
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass
class PageFetchResult:
    url: str
    text: str
    title: str = ""
    error: str | None = None


@dataclass
class TextCandidate:
    priority: int
    parts: list[str]


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


def fetch_page_text(url: str, timeout_seconds: int, max_chars: int) -> PageFetchResult:
    request = Request(
        url,
        headers={
            "User-Agent": "raindian/0.1 (+https://github.com/) Python urllib",
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1",
        },
        method="GET",
    )
    try:
        context = ssl.create_default_context()
        with urlopen(request, timeout=timeout_seconds, context=context) as response:
            content_type = response.headers.get("Content-Type", "")
            raw = response.read(max_chars * 4)
    except HTTPError as exc:
        return PageFetchResult(url=url, text="", error=f"HTTP {exc.code}")
    except URLError as exc:
        return PageFetchResult(url=url, text="", error=str(exc.reason))
    except Exception as exc:
        return PageFetchResult(url=url, text="", error=str(exc))

    if "text/html" not in content_type and "text/plain" not in content_type and "application/xhtml" not in content_type:
        return PageFetchResult(url=url, text="", error=f"unsupported content type: {content_type or 'unknown'}")

    encoding = charset_from_content_type(content_type) or "utf-8"
    decoded = raw.decode(encoding, errors="replace")
    if "text/plain" in content_type:
        return PageFetchResult(url=url, text=normalize_plain_text(decoded)[:max_chars], title="")

    parser = TextExtractor()
    parser.feed(decoded)
    parser.close()
    return PageFetchResult(url=url, text=parser.text[:max_chars], title=parser.title)


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
