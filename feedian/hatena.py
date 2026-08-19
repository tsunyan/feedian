from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import re
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPSHandler, Request, build_opener
from xml.etree import ElementTree

from .canonical import CanonicalItem, source_id_for_url, url_content_key
from .extract import SafeRedirectHandler, validate_fetch_url
from .retry import run_with_retries


SEARCH_API_URL = "https://b.hatena.ne.jp/my/search/json"
ENTRY_INFO_API_URL = "https://b.hatena.ne.jp/entry/jsonlite/"
ENTRY_COUNT_API_URL = "https://bookmark.hatenaapis.com/count/entries"
STAR_ENTRY_API_URL = "https://s.hatena.ne.jp/entry.json"
# The search API requires a non-empty full-text query. Searching both URL
# schemes covers the HTTP(S) bookmark URLs Feedian can turn into web notes.
SEARCH_QUERIES = ("https", "http")


@dataclass(frozen=True)
class HatenaPublicComment:
    user: str
    comment: str
    tags: list[str] = field(default_factory=list)
    timestamp: str = ""
    star_count: int | None = None


@dataclass(frozen=True)
class HatenaEntryDiscussion:
    entry_url: str = ""
    bookmark_count: int = 0
    entry_id: str = ""
    comments: list[HatenaPublicComment] = field(default_factory=list)


def fetch_hatena_entry_discussion(
    url: str,
    *,
    timeout_seconds: int = 30,
    max_retries: int = 3,
    retry_base_seconds: float = 1.0,
) -> HatenaEntryDiscussion:
    request = Request(
        f"{ENTRY_INFO_API_URL}?{urlencode({'url': url})}",
        headers={
            "Accept": "application/json",
            "User-Agent": "feedian/0.1 (+https://github.com/tsunyan/feedian)",
        },
        method="GET",
    )
    try:
        data = run_with_retries(
            lambda: _read_entry_json(request, timeout_seconds),
            max_retries=max_retries,
            retry_base_seconds=retry_base_seconds,
        )
    except HTTPError as exc:
        raise RuntimeError(f"Hatena entry API HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"Hatena entry API network error: {exc.reason}") from exc
    raw_comments = data.get("bookmarks")
    entry_id = str(data.get("eid") or "").strip()
    comments: list[HatenaPublicComment] = []
    if isinstance(raw_comments, list):
        for raw in raw_comments:
            if not isinstance(raw, dict):
                continue
            comment = str(raw.get("comment") or "").strip()
            if not comment:
                continue
            raw_tags = raw.get("tags")
            tags = [str(tag).strip() for tag in raw_tags if str(tag).strip()] if isinstance(raw_tags, list) else []
            comments.append(
                HatenaPublicComment(
                    user=str(raw.get("user") or "").strip(),
                    comment=comment,
                    tags=tags,
                    timestamp=str(raw.get("timestamp") or "").strip(),
                )
            )
    count = data.get("count")
    return HatenaEntryDiscussion(
        entry_url=str(data.get("entry_url") or "").strip(),
        bookmark_count=count if isinstance(count, int) and count >= 0 else 0,
        entry_id=entry_id,
        comments=comments,
    )


def fetch_hatena_bookmark_counts(
    urls: list[str],
    *,
    timeout_seconds: int = 30,
    batch_size: int = 50,
    workers: int = 4,
    max_retries: int = 3,
    retry_base_seconds: float = 1.0,
) -> dict[str, int]:
    """Fetch only public bookmark counts, at most 50 URLs per official API request."""
    unique_urls = list(dict.fromkeys(url for url in urls if url))
    counts = {url: 0 for url in unique_urls}
    size = max(1, min(50, batch_size))
    batches = [unique_urls[offset : offset + size] for offset in range(0, len(unique_urls), size)]

    def fetch_batch(batch: list[str]) -> dict[str, Any]:
        request = Request(
            f"{ENTRY_COUNT_API_URL}?{urlencode([('url', url) for url in batch])}",
            headers={
                "Accept": "application/json",
                "User-Agent": "feedian/0.1 (+https://github.com/tsunyan/feedian)",
            },
            method="GET",
        )
        return run_with_retries(
            lambda: _read_entry_json(request, timeout_seconds),
            max_retries=max_retries,
            retry_base_seconds=retry_base_seconds,
        )

    worker_count = max(1, min(len(batches) or 1, workers))
    try:
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="feedian-comment-counts") as executor:
            for data in executor.map(fetch_batch, batches):
                for url, value in data.items():
                    if url in counts and isinstance(value, int) and value >= 0:
                        counts[url] = value
    except HTTPError as exc:
        raise RuntimeError(f"Hatena bookmark count API HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"Hatena bookmark count API network error: {exc.reason}") from exc
    return counts


def fetch_hatena_star_counts(
    uris: list[str], *, timeout_seconds: int = 30, batch_size: int = 40, workers: int = 4
) -> dict[str, int]:
    unique_uris = list(dict.fromkeys(uri for uri in uris if uri))
    # The API omits requested URIs that have no public stars. They are valid zeroes,
    # not failed lookups.
    counts: dict[str, int] = {uri: 0 for uri in unique_uris}
    batches = [
        unique_uris[offset : offset + max(1, batch_size)]
        for offset in range(0, len(unique_uris), max(1, batch_size))
    ]

    def fetch_batch(batch: list[str]) -> dict[str, Any]:
        request = Request(
            f"{STAR_ENTRY_API_URL}?{urlencode([('uri', uri) for uri in batch])}",
            headers={"Accept": "application/json", "User-Agent": "feedian/0.1 (+https://github.com/tsunyan/feedian)"},
            method="GET",
        )
        return _read_entry_json(request, timeout_seconds)

    worker_count = max(1, min(len(batches) or 1, workers))
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="feedian-stars") as executor:
        responses = executor.map(fetch_batch, batches)
        for data in responses:
            entries = data.get("entries")
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("uri"), str):
                    continue
                total = _star_objects_count(entry.get("stars"))
                colored = entry.get("colored_stars")
                if isinstance(colored, list):
                    for group in colored:
                        if isinstance(group, dict):
                            total += _star_objects_count(group.get("stars"))
                counts[str(entry["uri"])] = total
    return counts


def hatena_comment_star_url(user: str, timestamp: str, entry_id: str) -> str:
    date = "".join(character for character in timestamp[:10] if character.isdigit())
    if not user or len(date) != 8 or not entry_id:
        return ""
    return f"https://b.hatena.ne.jp/{user}/{date}#bookmark-{entry_id}"


def _star_objects_count(value: object) -> int:
    if not isinstance(value, list):
        return 0
    total = 0
    for star in value:
        if not isinstance(star, dict):
            continue
        count = star.get("count", 1)
        total += count if isinstance(count, int) and count > 0 else 1
    return total


def _read_entry_json(request: Request, timeout_seconds: int) -> dict[str, Any]:
    with build_opener(HTTPSHandler(context=ssl.create_default_context())).open(
        request,
        timeout=timeout_seconds,
    ) as response:
        data = json.loads(response.read().decode("utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise RuntimeError("Hatena entry API returned an invalid JSON object.")
    return data


def fetch_hatena_bookmarks(
    username: str,
    api_key: str,
    *,
    limit: int | None = None,
    timeout_seconds: int = 30,
    max_retries: int = 3,
    retry_base_seconds: float = 1.0,
    request_interval_seconds: float = 0.3,
    on_page: Callable[[int, int], None] | None = None,
    known: set[str] | None = None,
    stop_after_known_pages: int = 0,
    on_stopped_early: Callable[[], None] | None = None,
) -> list[CanonicalItem]:
    """Collect bookmarks through the authenticated search API.

    A non-zero stop_after_known_pages turns on quick collection: each query
    stops once that many consecutive pages held nothing outside `known`. The
    search API returns each query in non-increasing timestamp order, measured
    over all 6,987 rows and 69 page boundaries of the reference vault, so new
    bookmarks sit at the head. See docs/specs/20260819-sync-ingest-throughput.ja.md.
    """
    quick = stop_after_known_pages > 0
    known_ids = known or set()
    # The two searches overlap, and the same bookmark can land in both. Counting
    # it once per appearance would spend the limit on duplicates and stop before
    # reaching new bookmarks further down.
    seen_new: set[str] = set()
    items_by_id: dict[str, CanonicalItem] = {}
    examined = 0
    known_total = 0
    new_items = 0
    stopped_early = False
    page_size = 100
    next_request_at = 0.0
    for search_query in SEARCH_QUERIES:
        offset = 0
        query_total: int | None = None
        # Counted per query, never shared. Which query a new bookmark's text
        # lands in is not knowable ahead of time, so one query stopping on a
        # page of known items says nothing about the other.
        consecutive_known_pages = 0
        while True:
            if quick:
                # limit counts the items quick actually takes on, as it does for
                # Raindrop. Comparing it against the offset instead would stop a
                # query before reaching new items that sort below known ones.
                if limit is not None and new_items >= limit:
                    break
                requested = page_size
            else:
                remaining = None if limit is None else max(0, limit - offset)
                requested = page_size if remaining is None else min(page_size, remaining)
                if requested <= 0:
                    break
            now = time.monotonic()
            if next_request_at > now:
                time.sleep(next_request_at - now)
            next_request_at = max(now, next_request_at) + max(0.0, request_interval_seconds)
            query = urlencode({"q": search_query, "of": offset, "limit": requested})
            request = Request(
                f"{SEARCH_API_URL}?{query}",
                headers={
                    "Accept": "application/json",
                    "User-Agent": "feedian/0.1 (+https://github.com/tsunyan/feedian)",
                    "X-WSSE": _wsse_header(username, api_key),
                },
                method="GET",
            )
            try:
                data = run_with_retries(
                    lambda: _read_json(request, timeout_seconds),
                    max_retries=max_retries,
                    retry_base_seconds=retry_base_seconds,
                )
            except HTTPError as exc:
                if exc.code in {401, 403}:
                    raise RuntimeError("Hatena authentication failed; check HATENA_ID and HATENA_API_KEY.") from exc
                raise RuntimeError(f"Hatena search API HTTP {exc.code}") from exc
            except URLError as exc:
                raise RuntimeError(f"Hatena search API network error: {exc.reason}") from exc

            rows, total = _validate_search_response(data)
            if query_total is None:
                query_total = total
                known_total += min(total, limit) if limit is not None else total
            page_items = 0
            page_new = 0
            for row in rows:
                item = _search_result_item(row)
                if item is not None:
                    items_by_id[item.source_id] = item
                    page_items += 1
                    if item.source_id not in known_ids:
                        # The page holds something outside the vault, so it is not
                        # a known page - whichever query surfaced it first.
                        page_new += 1
                        if item.source_id not in seen_new:
                            seen_new.add(item.source_id)
                            new_items += 1
            examined += len(rows)
            target_total = known_total
            if on_page is not None:
                on_page(examined, target_total)
            offset += len(rows)
            if not rows or offset >= total or len(rows) < requested:
                break
            if quick:
                # A page that parsed nothing carries no evidence either way, so
                # it neither confirms nor breaks the streak of known pages.
                if page_items == 0:
                    consecutive_known_pages = 0
                elif page_new == 0:
                    consecutive_known_pages += 1
                    if consecutive_known_pages >= stop_after_known_pages:
                        stopped_early = True
                        break
                else:
                    consecutive_known_pages = 0

    if stopped_early and on_stopped_early is not None:
        on_stopped_early()
    items = sorted(items_by_id.values(), key=lambda item: item.created_at, reverse=True)
    # Quick hands every collected item downstream, known ones included, because
    # sync.py is what skips them and counts them as skipped. Truncating here
    # would cut by total count rather than by the new items limit means.
    return items if quick else items[:limit]


def _validate_search_response(data: dict[str, Any]) -> tuple[list[Any], int]:
    meta = data.get("meta")
    status = meta.get("status") if isinstance(meta, dict) else None
    if status in {401, 403, "401", "403"}:
        raise RuntimeError("Hatena authentication failed; check HATENA_ID and HATENA_API_KEY.")
    rows = data.get("bookmarks")
    raw_total = meta.get("total") if isinstance(meta, dict) else None
    if not isinstance(rows, list) or not isinstance(raw_total, (int, str)) or not str(raw_total).isdigit():
        raise RuntimeError("Hatena search API returned an invalid response.")
    return rows, int(raw_total)


def _read_json(request: Request, timeout_seconds: int) -> dict[str, Any]:
    with build_opener(HTTPSHandler(context=ssl.create_default_context())).open(
        request,
        timeout=timeout_seconds,
    ) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("Hatena search API returned an invalid JSON object.")
    return data


def _wsse_header(username: str, api_key: str) -> str:
    # SHA-1 is fixed by the WSSE UsernameToken profile, which Hatena's API implements:
    # the server recomputes Base64(SHA1(nonce + created + key)) to verify. A stronger
    # digest here fails authentication. CodeQL flags this as weak password hashing
    # (py/weak-sensitive-data-hashing); the rule targets password storage, where an
    # attacker brute-forces a stolen digest offline. This one goes over HTTPS with a
    # fresh 20-byte nonce, and recovering the key from it needs a SHA-1 preimage.
    nonce = os.urandom(20)
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = hashlib.sha1(nonce + created.encode("utf-8") + api_key.encode("utf-8")).digest()
    return (
        f'UsernameToken Username="{username}", '
        f'PasswordDigest="{base64.b64encode(digest).decode("ascii")}", '
        f'Nonce="{base64.b64encode(nonce).decode("ascii")}", Created="{created}"'
    )


def _search_result_item(row: Any) -> CanonicalItem | None:
    if not isinstance(row, dict):
        return None
    entry = row.get("entry")
    if not isinstance(entry, dict):
        return None
    url = str(entry.get("url") or "").strip()
    if not url:
        return None
    tags, comment = _split_hatena_comment(str(row.get("comment") or ""))
    timestamp = row.get("timestamp")
    created = ""
    if isinstance(timestamp, (int, float)) and timestamp >= 0:
        created = datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
    return CanonicalItem(
        source="hatena",
        source_id=source_id_for_url("hatena", url),
        content_key=url_content_key(url),
        url=url,
        title=str(entry.get("title") or "").strip(),
        excerpt=clean_hatena_excerpt(url, str(entry.get("snippet") or "")),
        comment=comment,
        tags=tags,
        created_at=created,
        private=_hatena_bool(row.get("is_private")),
        item_type="bookmark",
    )


def _split_hatena_comment(value: str) -> tuple[list[str], str]:
    tags: list[str] = []
    remaining = value.strip()
    while True:
        match = re.match(r"^\[([^\[\]]+)\]", remaining)
        if match is None:
            break
        tags.append(match.group(1).strip())
        remaining = remaining[match.end() :].lstrip()
    return tags, remaining


def clean_hatena_excerpt(url: str, excerpt: str) -> str:
    text = excerpt.strip()
    hostname = (urlparse(url).hostname or "").lower()
    if hostname == "dic.nicovideo.jp" or hostname.endswith(".dic.nicovideo.jp"):
        text = re.sub(
            r"(?is)\s*sponsored\s+by\s+求人ボックス\b.*?(?=概要|$)",
            " ",
            text,
        )
    return re.sub(r"\s+", " ", text).strip()


def _hatena_bool(value: Any) -> bool:
    return value not in {None, 0, "0", ""}


def load_hatena_export(
    location: str,
    *,
    timeout_seconds: int = 30,
    allow_private_urls: bool = False,
) -> list[CanonicalItem]:
    data = _read_export(location, timeout_seconds, allow_private_urls)
    stripped = data.lstrip()
    if stripped.startswith(b"<"):
        try:
            root = ElementTree.fromstring(data)
        except ElementTree.ParseError:
            return _parse_bookmark_html(data.decode("utf-8", errors="replace"))
        root_name = _local_name(root.tag).lower()
        if root_name == "feed":
            return _deduplicate(_parse_atom(root))
        if root_name in {"rdf", "rss"}:
            return _deduplicate(_parse_rss(root))
        if root_name == "html":
            return _deduplicate(_parse_bookmark_html(data.decode("utf-8", errors="replace")))
    raise ValueError("Unsupported Hatena export format; use Atom, RSS 1.0, or bookmark HTML.")


def _read_export(location: str, timeout_seconds: int, allow_private_urls: bool) -> bytes:
    parsed = urlparse(location)
    if parsed.scheme.lower() not in {"http", "https"}:
        return Path(location).expanduser().read_bytes()
    validate_fetch_url(location, allow_private_urls=allow_private_urls)
    request = Request(
        location,
        headers={"User-Agent": "feedian/0.1 (+https://github.com/tsunyan/feedian)"},
        method="GET",
    )
    try:
        opener = build_opener(
            HTTPSHandler(context=ssl.create_default_context()),
            SafeRedirectHandler(allow_private_urls),
        )
        with opener.open(request, timeout=timeout_seconds) as response:
            return response.read()
    except HTTPError as exc:
        raise RuntimeError(f"Hatena export HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"Hatena export network error: {exc.reason}") from exc


def _parse_atom(root: ElementTree.Element) -> list[CanonicalItem]:
    items: list[CanonicalItem] = []
    for entry in _descendants(root, "entry"):
        url = _atom_link(entry)
        if not url:
            continue
        items.append(
            _item(
                url=url,
                title=_child_text(entry, "title"),
                comment=_child_text(entry, "summary") or _child_text(entry, "content"),
                tags=_entry_tags(entry),
                created=_child_text(entry, "published") or _child_text(entry, "issued"),
                updated=_child_text(entry, "updated") or _child_text(entry, "modified"),
                private=_private_value(entry),
            )
        )
    return items


def _parse_rss(root: ElementTree.Element) -> list[CanonicalItem]:
    items: list[CanonicalItem] = []
    for entry in _descendants(root, "item"):
        url = _child_text(entry, "link") or entry.attrib.get("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about", "")
        if not url:
            continue
        items.append(
            _item(
                url=url,
                title=_child_text(entry, "title"),
                comment=_child_text(entry, "description"),
                tags=[element.text.strip() for element in _children(entry, "subject") if element.text and element.text.strip()],
                created=_child_text(entry, "date"),
                updated="",
                private=_private_value(entry),
            )
        )
    return items


def _item(
    *,
    url: str,
    title: str,
    comment: str,
    tags: list[str],
    created: str,
    updated: str,
    private: bool | None,
) -> CanonicalItem:
    return CanonicalItem(
        source="hatena",
        source_id=source_id_for_url("hatena", url),
        content_key=url_content_key(url),
        url=url.strip(),
        title=title.strip(),
        comment=comment.strip(),
        tags=tags,
        created_at=created.strip(),
        updated_at=updated.strip(),
        private=private,
        item_type="bookmark",
    )


def _atom_link(entry: ElementTree.Element) -> str:
    fallback = ""
    for link in _children(entry, "link"):
        href = (link.attrib.get("href") or (link.text or "")).strip()
        if not href:
            continue
        rel = link.attrib.get("rel", "alternate")
        if rel in {"alternate", "related"}:
            return href
        fallback = fallback or href
    return fallback


def _entry_tags(entry: ElementTree.Element) -> list[str]:
    tags = [element.text.strip() for element in _children(entry, "subject") if element.text and element.text.strip()]
    tags.extend(
        element.attrib["term"].strip()
        for element in _children(entry, "category")
        if element.attrib.get("term", "").strip()
    )
    return list(dict.fromkeys(tags))


def _private_value(entry: ElementTree.Element) -> bool | None:
    for element in entry.iter():
        if _local_name(element.tag).lower() != "private":
            continue
        value = (element.text or "").strip().lower()
        return value in {"1", "true", "yes"}
    return None


def _child_text(element: ElementTree.Element, name: str) -> str:
    child = next(iter(_children(element, name)), None)
    return "" if child is None else "".join(child.itertext()).strip()


def _children(element: ElementTree.Element, name: str) -> list[ElementTree.Element]:
    return [child for child in list(element) if _local_name(child.tag).lower() == name.lower()]


def _descendants(element: ElementTree.Element, name: str) -> list[ElementTree.Element]:
    return [child for child in element.iter() if _local_name(child.tag).lower() == name.lower()]


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def _deduplicate(items: list[CanonicalItem]) -> list[CanonicalItem]:
    unique: dict[str, CanonicalItem] = {}
    for item in items:
        unique[item.source_id] = item
    return list(unique.values())


class _BookmarkHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[dict[str, Any]] = []
        self._current: dict[str, Any] | None = None
        self._in_anchor = False
        self._in_description = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        if tag.lower() == "a" and values.get("href"):
            self._in_description = False
            self._current = {"url": values["href"], "title": [], "comment": [], "attrs": values}
            self._in_anchor = True
        elif tag.lower() == "dd" and self.items:
            self._in_description = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._current is not None:
            self.items.append(self._current)
            self._current = None
            self._in_anchor = False
        elif tag.lower() == "dd":
            self._in_description = False

    def handle_data(self, data: str) -> None:
        if self._in_anchor and self._current is not None:
            self._current["title"].append(data)
        elif self._in_description and self.items:
            self.items[-1]["comment"].append(data)


def _parse_bookmark_html(text: str) -> list[CanonicalItem]:
    parser = _BookmarkHTMLParser()
    parser.feed(text)
    parser.close()
    items: list[CanonicalItem] = []
    for raw in parser.items:
        attrs = raw["attrs"]
        created = ""
        if attrs.get("add_date", "").isdigit():
            created = datetime.fromtimestamp(int(attrs["add_date"]), tz=timezone.utc).isoformat()
        tags = [tag.strip() for tag in attrs.get("tags", "").split(",") if tag.strip()]
        private_value = attrs.get("private", "").strip().lower()
        items.append(
            _item(
                url=raw["url"],
                title="".join(raw["title"]),
                comment="".join(raw["comment"]),
                tags=tags,
                created=created,
                updated="",
                private=private_value in {"1", "true", "yes"} if private_value else None,
            )
        )
    return items
