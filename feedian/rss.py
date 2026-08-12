from __future__ import annotations

import hashlib
import ssl
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import urljoin, urlsplit
from urllib.error import HTTPError
from urllib.request import HTTPSHandler, Request, build_opener

from lxml import html as lxml_html

from .canonical import CanonicalItem, url_content_key
from .extract import SafeRedirectHandler, validate_fetch_url
from .retry import run_with_retries
from .store import stable_json


FEED_XML_MAX_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class RssItem:
    item: CanonicalItem
    payload: bytes


def fetch_rss_items(
    feed_url: str,
    *,
    timeout_seconds: int = 30,
    allow_private_urls: bool = False,
    name: str = "",
    folder: str = "",
    tags: list[str] | None = None,
    route: str = "",
    category_routes: dict[str, str] | None = None,
    etag: str = "",
    last_modified: str = "",
    max_retries: int = 3,
    retry_base_seconds: float = 1.0,
) -> list[RssItem]:
    validate_fetch_url(feed_url, allow_private_urls=allow_private_urls)
    headers = {"User-Agent": "feedian/0.1 (+https://github.com/tsunyan/feedian)"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    request = Request(feed_url, headers=headers)
    opener = build_opener(HTTPSHandler(context=ssl.create_default_context()), SafeRedirectHandler(allow_private_urls))

    def read_feed() -> tuple[bytes, str, str, str]:
        try:
            with opener.open(request, timeout=timeout_seconds) as response:
                raw_body = response.read(FEED_XML_MAX_BYTES + 1)
                final = response.geturl()
                response_etag = str(response.headers.get("ETag") or "")
                response_last_modified = str(response.headers.get("Last-Modified") or "")
                return raw_body, final, response_etag, response_last_modified
        except HTTPError as exc:
            if exc.code == 304:
                exc.close()
                return b"", feed_url, etag, last_modified
            raise

    raw, final_url, response_etag, response_last_modified = run_with_retries(
        read_feed,
        max_retries=max_retries,
        retry_base_seconds=retry_base_seconds,
    )
    if not raw:
        return []
    if len(raw) > FEED_XML_MAX_BYTES:
        raise RuntimeError("RSS feed exceeds the 10 MiB XML limit.")
    items = parse_rss_items(
        raw,
        final_url,
        identity_feed_url=feed_url,
        configured_name=name,
        feed_folder=folder,
        feed_tags=tags,
        route=route,
        category_routes=category_routes,
    )
    for entry in items:
        entry.item.provider_metadata.update(
            {"feed_etag": response_etag, "feed_last_modified": response_last_modified}
        )
    return items


def parse_rss_items(
    raw: bytes,
    feed_url: str,
    *,
    identity_feed_url: str | None = None,
    configured_name: str = "",
    feed_folder: str = "",
    feed_tags: list[str] | None = None,
    route: str = "",
    category_routes: dict[str, str] | None = None,
) -> list[RssItem]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise RuntimeError(f"Could not parse RSS/Atom feed: {exc}") from exc

    identity_url = identity_feed_url or feed_url
    atom = _local_name(root.tag) == "feed"
    channel = root if atom else (_first_descendant(root, "channel") or root)
    feed_title = configured_name or _child_text(channel, "title")
    feed_site = _entry_link(channel, atom=atom, base_url=feed_url)
    resolved_folder = feed_folder or feed_title or _default_feed_folder(identity_url)
    entries = _direct_children(root, "entry") if atom else list(_descendants(root, "item"))
    result: list[RssItem] = []

    for entry in entries:
        title = _child_text(entry, "title")
        link = _entry_link(entry, atom=atom, base_url=feed_url)
        native = _child_text(entry, "id" if atom else "guid")
        if not link and not atom:
            guid = _first_child(entry, "guid")
            guid_text = _element_text(guid)
            if guid is not None and guid.get("isPermaLink", "true").lower() != "false":
                link = urljoin(feed_url, guid_text)
        if not link and atom and _looks_like_http_url(native):
            link = native
        if not _looks_like_http_url(link):
            continue
        native = native or link or title
        if not native:
            continue

        summary_html = _child_text(entry, "summary" if atom else "description")
        content_html = _child_text(entry, "content") if atom else _child_text(entry, "encoded")
        embedded_content = _plain_text(content_html or summary_html)
        excerpt = _plain_text(summary_html or content_html)
        entry_tags = _entry_categories(entry, atom=atom)
        all_tags = _unique_tags([*(feed_tags or []), *entry_tags])
        published = (
            _child_text(entry, "published" if atom else "pubDate")
            or _child_text(entry, "updated")
            or _child_text(entry, "date")
        )
        updated = _child_text(entry, "updated") if atom else ""
        selected_route = route or _category_route(entry_tags, category_routes or {})
        source_id = "rss-" + hashlib.sha256(f"{identity_url}\0{native}".encode("utf-8")).hexdigest()[:20]
        provider_metadata = {
            "feed_url": identity_url,
            "feed_final_url": feed_url,
            "feed_title": feed_title,
            "feed_site": feed_site,
            "feed_folder": resolved_folder,
            "feed_route": selected_route,
            "entry_id": native,
            "published_at": published,
        }
        item = CanonicalItem(
            source="rss",
            source_id=source_id,
            content_key=url_content_key(link),
            url=link,
            title=title,
            excerpt=excerpt,
            tags=all_tags,
            created_at=published,
            updated_at=updated,
            provider_metadata=provider_metadata,
            embedded_content=embedded_content,
        )
        payload = {
            **provider_metadata,
            "native_id": native,
            "title": title,
            "link": link,
            "summary": summary_html,
            "content": content_html,
            "tags": all_tags,
            "updated_at": updated,
        }
        result.append(RssItem(item, stable_json(payload).encode("utf-8")))
    return result


def published_timestamp(item: RssItem) -> float | None:
    value = item.item.created_at.strip()
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _direct_children(entry: ET.Element, name: str) -> list[ET.Element]:
    wanted = name.lower()
    return [child for child in list(entry) if _local_name(child.tag) == wanted]


def _descendants(entry: ET.Element, name: str):
    wanted = name.lower()
    return (child for child in entry.iter() if child is not entry and _local_name(child.tag) == wanted)


def _first_descendant(entry: ET.Element, name: str) -> ET.Element | None:
    return next(_descendants(entry, name), None)


def _first_child(entry: ET.Element, name: str) -> ET.Element | None:
    children = _direct_children(entry, name)
    return children[0] if children else None


def _element_text(element: ET.Element | None) -> str:
    return "" if element is None else "".join(element.itertext()).strip()


def _child_text(entry: ET.Element, name: str) -> str:
    return _element_text(_first_child(entry, name))


def _entry_link(entry: ET.Element, *, atom: bool, base_url: str) -> str:
    links = _direct_children(entry, "link")
    if not links:
        return ""
    if not atom:
        return urljoin(base_url, _element_text(links[0]))
    selected = next(
        (
            node
            for node in links
            if node.get("rel", "alternate").lower() == "alternate"
            and (not node.get("type") or "html" in node.get("type", "").lower())
        ),
        next((node for node in links if node.get("rel", "alternate").lower() == "alternate"), links[0]),
    )
    return urljoin(base_url, selected.get("href", "").strip())


def _entry_categories(entry: ET.Element, *, atom: bool) -> list[str]:
    values: list[str] = []
    for category in _direct_children(entry, "category"):
        value = category.get("term", "") if atom else _element_text(category)
        if value.strip():
            values.append(value.strip())
    return _unique_tags(values)


def _category_route(tags: list[str], routes: dict[str, str]) -> str:
    normalized = {key.casefold(): value for key, value in routes.items()}
    return next((normalized[tag.casefold()] for tag in tags if tag.casefold() in normalized), "")


def _unique_tags(tags: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in tags:
        tag = str(value).strip()
        key = tag.casefold()
        if tag and key not in seen:
            seen.add(key)
            result.append(tag)
    return result


def _default_feed_folder(feed_url: str) -> str:
    parsed = urlsplit(feed_url)
    hostname = parsed.hostname or "RSS Feed"
    leaf = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    if leaf.lower() in {"", "feed", "rss", "atom", "index.xml", "feed.xml", "rss.xml", "atom.xml"}:
        return hostname
    stem = leaf.rsplit(".", 1)[0]
    return f"{hostname} - {stem}" if stem else hostname


def _looks_like_http_url(value: str) -> bool:
    return urlsplit(value.strip()).scheme.lower() in {"http", "https"}


def _plain_text(value: str) -> str:
    if not value:
        return ""
    try:
        return " ".join(lxml_html.fromstring(f"<div>{value}</div>").text_content().split())
    except (TypeError, ValueError):
        return " ".join(unescape(value).split())
