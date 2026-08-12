from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit


@dataclass(frozen=True)
class CanonicalItem:
    source: str
    source_id: str
    content_key: str
    url: str
    title: str = ""
    excerpt: str = ""
    comment: str = ""
    tags: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    private: bool | None = None
    item_type: str = "link"
    provider_metadata: dict[str, Any] = field(default_factory=dict)
    embedded_content: str = ""

    def as_bookmark_metadata(self) -> dict[str, object]:
        hostname = urlsplit(self.url).hostname or ""
        metadata = {
            "_id": self.source_id,
            "_feedian_source": self.source,
            "_feedian_source_id": self.source_id,
            "_feedian_content_key": self.content_key,
            "title": self.title,
            "link": self.url,
            "domain": hostname,
            "type": self.item_type,
            "tags": list(self.tags),
            "excerpt": self.excerpt,
            "note": self.comment,
            "created": self.created_at,
            "lastUpdate": self.updated_at,
            "private": self.private,
        }
        if self.provider_metadata:
            metadata["_feedian_provider_metadata"] = dict(self.provider_metadata)
        return metadata


def canonicalize_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    port = parsed.port
    if port is not None and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        hostname = f"{hostname}:{port}"
    return urlunsplit((scheme, hostname, parsed.path or "/", parsed.query, ""))


def url_content_key(url: str) -> str:
    normalized = canonicalize_url(url)
    return "url:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def source_id_for_url(source: str, url: str) -> str:
    digest = hashlib.sha256(canonicalize_url(url).encode("utf-8")).hexdigest()[:16]
    return f"{source}-{digest}"


def canonical_item_from_metadata(
    item: dict[str, Any],
    *,
    default_source: str = "raindrop",
) -> CanonicalItem:
    url = str(item.get("link") or "")
    source = str(item.get("_feedian_source") or default_source)
    source_id = str(item.get("_feedian_source_id") or item.get("_id") or "unknown")
    content_key = str(item.get("_feedian_content_key") or (url_content_key(url) if url else ""))
    return CanonicalItem(
        source=source,
        source_id=source_id,
        content_key=content_key,
        url=url,
        title=str(item.get("title") or ""),
        excerpt=str(item.get("excerpt") or ""),
        comment=str(item.get("note") or ""),
        tags=[str(tag) for tag in item.get("tags") or []],
        created_at=str(item.get("created") or ""),
        updated_at=str(item.get("lastUpdate") or ""),
        private=item.get("private") if isinstance(item.get("private"), bool) else None,
        item_type=str(item.get("type") or "link"),
        provider_metadata=dict(item.get("_feedian_provider_metadata") or {}),
    )
