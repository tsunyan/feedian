from __future__ import annotations

import hashlib
import ssl
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html import unescape
from urllib.request import HTTPSHandler, Request, build_opener

from lxml import html as lxml_html

from .canonical import CanonicalItem, url_content_key
from .extract import SafeRedirectHandler, validate_fetch_url
from .store import stable_json


@dataclass(frozen=True)
class RssItem:
    item: CanonicalItem
    payload: bytes


def fetch_rss_items(feed_url: str, *, timeout_seconds: int = 30, allow_private_urls: bool = False) -> list[RssItem]:
    validate_fetch_url(feed_url, allow_private_urls=allow_private_urls)
    request = Request(feed_url, headers={"User-Agent": "feedian/0.1 (+https://github.com/) Python urllib"})
    opener = build_opener(HTTPSHandler(context=ssl.create_default_context()), SafeRedirectHandler(allow_private_urls))
    with opener.open(request, timeout=timeout_seconds) as response:
        raw = response.read(10 * 1024 * 1024 + 1)
        final_url = response.geturl()
    if len(raw) > 10 * 1024 * 1024:
        raise RuntimeError("RSS feed exceeds the 10 MiB XML limit.")
    return parse_rss_items(raw, final_url)


def parse_rss_items(raw: bytes, feed_url: str) -> list[RssItem]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise RuntimeError(f"Could not parse RSS/Atom feed: {exc}") from exc
    entries = root.findall(".//item")
    atom = False
    if not entries:
        entries = root.findall(".//{http://www.w3.org/2005/Atom}entry")
        atom = True
    result: list[RssItem] = []
    for entry in entries:
        title = _child_text(entry, "title", atom=atom)
        link = _entry_link(entry, atom=atom)
        native = _child_text(entry, "guid", atom=False) or _child_text(entry, "id", atom=True) or link or title
        if not native or not link:
            continue
        summary = _child_text(entry, "description", atom=False) or _child_text(entry, "summary", atom=True) or _child_text(entry, "content", atom=True)
        category_tag = "{http://www.w3.org/2005/Atom}category" if atom else "category"
        tags = [category.get("term", "") if atom else (category.text or "") for category in entry.findall(category_tag)]
        published = _child_text(entry, "pubDate", atom=False) or _child_text(entry, "published", atom=True) or _child_text(entry, "updated", atom=True)
        source_id = "rss-" + hashlib.sha256(f"{feed_url}\0{native}".encode("utf-8")).hexdigest()[:20]
        item = CanonicalItem(
            source="rss", source_id=source_id, content_key=url_content_key(link), url=link, title=title,
            excerpt=_plain_text(summary), tags=[tag.strip() for tag in tags if tag and tag.strip()], created_at=published,
        )
        payload = {"feed_url": feed_url, "native_id": native, "title": title, "link": link, "summary": summary, "tags": item.tags, "published": published}
        result.append(RssItem(item, stable_json(payload).encode("utf-8")))
    return result


def _child_text(entry: ET.Element, name: str, *, atom: bool) -> str:
    child = entry.find(f"{{http://www.w3.org/2005/Atom}}{name}" if atom else name)
    return (child.text or "").strip() if child is not None else ""


def _entry_link(entry: ET.Element, *, atom: bool) -> str:
    if not atom:
        return _child_text(entry, "link", atom=False)
    links = entry.findall("{http://www.w3.org/2005/Atom}link")
    selected = next((node for node in links if node.get("rel", "alternate") == "alternate"), links[0] if links else None)
    return (selected.get("href", "") if selected is not None else "").strip()


def _plain_text(value: str) -> str:
    try:
        return " ".join(lxml_html.fromstring(f"<div>{value}</div>").text_content().split())
    except (TypeError, ValueError):
        return " ".join(unescape(value).split())
