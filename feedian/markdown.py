from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from .extract import PageFetchResult
from .canonical import CanonicalItem
from .hatena import HatenaEntryDiscussion


WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "COM1",
    "COM2",
    "COM3",
    "COM4",
    "COM5",
    "COM6",
    "COM7",
    "COM8",
    "COM9",
    "LPT1",
    "LPT2",
    "LPT3",
    "LPT4",
    "LPT5",
    "LPT6",
    "LPT7",
    "LPT8",
    "LPT9",
}
MAX_FILENAME_TITLE_CHARS = 60
FEEDIAN_SUMMARY_START = "<!-- feedian:summary:start -->"
FEEDIAN_SUMMARY_END = "<!-- feedian:summary:end -->"


def note_filename(item: dict[str, Any], title: str | None = None) -> str:
    title = title or item.get("title") or item.get("link") or "Untitled"
    item_id = item.get("_id") or "unknown"
    base = sanitize_filename(str(title))
    if not base:
        base = "Untitled"
    base = base[:MAX_FILENAME_TITLE_CHARS].rstrip(" .") or "Untitled"
    return f"{base} - {item_id}.md"


def canonical_note_filename(item: CanonicalItem, title: str | None = None) -> str:
    return note_filename({"_id": item.source_id, "title": item.title, "link": item.url}, title=title)


def sanitize_filename(value: str) -> str:
    # Keep filenames portable across Windows, Obsidian sync folders, and other filesystems.
    value = unicodedata.normalize("NFC", value)
    safe_chars: list[str] = []
    for char in value:
        category = unicodedata.category(char)
        if category.startswith(("L", "M", "N")) or char in " -_.":
            safe_chars.append(char)
        else:
            safe_chars.append(" ")
    value = "".join(safe_chars)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if value.upper() in WINDOWS_RESERVED_NAMES:
        value = f"{value}_"
    return value


def render_note(
    item: dict[str, Any],
    page: PageFetchResult,
    summary: dict[str, Any],
    base_tags: list[str] | None,
    generated_at: str,
    model: str | None,
    comments_note: str | None = None,
) -> str:
    title = summary.get("note_title") or item.get("title") or page.title or item.get("link") or "Untitled"
    all_tags = merge_tags(base_tags or [], item.get("tags") or [], summary.get("tags") or [])
    frontmatter: dict[str, Any] = {
        "title": title,
        "source_title": item.get("title"),
        "source": item.get("link"),
        "raindrop_id": item.get("_id"),
        "raindrop_collection_id": (item.get("collection") or {}).get("$id"),
        "domain": item.get("domain"),
        "type": item.get("type"),
        "created": item.get("created"),
        "last_update": item.get("lastUpdate"),
        "summary_generated_at": generated_at,
        "summary_model": model,
        "tags": all_tags,
    }
    add_fetch_diagnostics(frontmatter, page)
    if model:
        frontmatter["llm_tags"] = merge_tags(summary.get("tags") or [])
    lines = ["---", yaml_frontmatter(frontmatter), "---", "", f"# {escape_markdown_heading(title)}", ""]
    lines.extend(
        [
            f"- Source: {item.get('link') or ''}",
            f"- Raindrop ID: {item.get('_id') or ''}",
            f"- Domain: {item.get('domain') or ''}",
            "",
            "## Summary",
            "",
            str(summary.get("summary") or "").strip(),
            "",
        ]
    )
    key_points = [str(point).strip() for point in summary.get("key_points") or [] if str(point).strip()]
    if key_points:
        lines.extend(["## Key Points", ""])
        lines.extend([f"- {point}" for point in key_points])
        lines.append("")

    lines.extend(["## Tags", "", " ".join(f"#{tag}" for tag in all_tags), ""])

    if comments_note:
        lines.extend(["## Comments", "", f"- [[{comments_note}]]", ""])

    excerpt = (item.get("excerpt") or "").strip()
    note = (item.get("note") or "").strip()
    if excerpt or note or page.error:
        lines.extend(["## Raindrop Metadata", ""])
        if excerpt:
            lines.extend(["### Excerpt (Original)", "", excerpt, ""])
        if note:
            lines.extend(["### Note (Original)", "", note, ""])
        if page.error:
            lines.extend(["### Fetch Warning", "", page.error, ""])

    extracted_content = page.text.strip()
    if extracted_content:
        lines.extend(["## Extracted Content (Original)", "", extracted_content, ""])

    return "\n".join(lines).rstrip() + "\n"


def render_canonical_note(
    item: CanonicalItem,
    page: PageFetchResult,
    summary: dict[str, Any],
    base_tags: list[str] | None,
    generated_at: str,
    model: str | None,
    comments_note: str | None = None,
) -> str:
    title = summary.get("note_title") or item.title or page.title or item.url or "Untitled"
    all_tags = merge_tags(base_tags or [], item.tags, summary.get("tags") or [])
    domain = urlsplit(item.url).hostname or ""
    frontmatter = {
        "title": title,
        "source_title": item.title,
        "source": item.url,
        "source_type": item.source,
        "source_id": item.source_id,
        "content_key": item.content_key,
        "domain": domain,
        "type": item.item_type,
        "created": item.created_at,
        "last_update": item.updated_at,
        "private": item.private,
        "tags": all_tags,
    }
    add_fetch_diagnostics(frontmatter, page)
    if model:
        frontmatter["summary_generated_at"] = generated_at
        frontmatter["summary_model"] = model
        frontmatter["llm_tags"] = merge_tags(summary.get("tags") or [])
    lines = ["---", yaml_frontmatter(frontmatter), "---", "", f"# {escape_markdown_heading(str(title))}", ""]
    lines.extend(
        [
            f"- Source: {item.url}",
            f"- Source type: {item.source}",
            f"- Source ID: {item.source_id}",
            f"- Domain: {domain}",
            "",
        ]
    )
    if model:
        lines.extend(["## Summary", "", str(summary.get("summary") or "").strip(), ""])
        key_points = [str(point).strip() for point in summary.get("key_points") or [] if str(point).strip()]
        if key_points:
            lines.extend(["## Key Points", ""])
            lines.extend([f"- {point}" for point in key_points])
            lines.append("")
    lines.extend(["## Tags", "", " ".join(f"#{tag}" for tag in all_tags), ""])
    if comments_note:
        lines.extend(["## Comments", "", f"- [[{comments_note}]]", ""])
    if item.excerpt or item.comment or page.error:
        lines.extend([f"## {item.source.title()} Metadata", ""])
        if item.excerpt:
            lines.extend(["### Excerpt (Original)", "", item.excerpt, ""])
        if item.comment:
            lines.extend(["### Comment (Original)", "", item.comment, ""])
        if page.error:
            lines.extend(["### Fetch Warning", "", page.error, ""])
    if page.text.strip():
        lines.extend(["## Extracted Content (Original)", "", page.text.strip(), ""])
    return "\n".join(lines).rstrip() + "\n"


def comments_note_filename(main_note: str) -> str:
    stem = main_note[:-3] if main_note.lower().endswith(".md") else main_note
    return f"{stem}.comments.md"


def render_comments_note(
    item: CanonicalItem,
    page: PageFetchResult,
    hatena: HatenaEntryDiscussion,
    *,
    main_note: str,
    generated_at: str,
) -> str:
    title = item.title or page.title or item.url or "Untitled"
    main_stem = main_note[:-3] if main_note.lower().endswith(".md") else main_note
    frontmatter = {
        "title": f"{title} — Comments",
        "source": item.url,
        "source_type": item.source,
        "source_id": item.source_id,
        "content_key": item.content_key,
        "type": "comments",
        "parent": f"[[{main_stem}]]",
        "discussion_fetched_at": generated_at,
        "page_discussion_chars": len(page.discussion_text),
        "hatena_bookmark_count": hatena.bookmark_count,
        "hatena_comment_count": len(hatena.comments),
        "tags": ["hatena", "comments"],
    }
    lines = [
        "---",
        yaml_frontmatter(frontmatter),
        "---",
        "",
        f"# {escape_markdown_heading(title)} — Comments",
        "",
        f"- Original note: [[{main_stem}]]",
        f"- Source: {item.url}",
    ]
    if hatena.entry_url:
        lines.append(f"- Hatena entry: {hatena.entry_url}")
    lines.append("")
    if page.discussion_text.strip():
        lines.extend(["## Page Replies (Original)", "", page.discussion_text.strip(), ""])
    if hatena.comments:
        lines.extend(["## Hatena Bookmark Comments", ""])
        for comment in hatena.comments:
            heading = comment.user or "unknown"
            if comment.timestamp:
                heading += f" · {comment.timestamp}"
            lines.extend([f"### {escape_markdown_heading(heading)}", ""])
            if comment.tags:
                lines.extend([f"- Tags: {' '.join(f'#{normalize_tag(tag)}' for tag in comment.tags)}", ""])
            lines.extend([comment.comment, ""])
    return "\n".join(lines).rstrip() + "\n"


def upsert_raindrop_summary(note: str, summary: str) -> str:
    """Replace only Feedian's managed summary block in a Raindrop note."""
    summary = summary.strip()
    block = "\n".join(
        [
            FEEDIAN_SUMMARY_START,
            "## Feedian Summary",
            "",
            summary,
            FEEDIAN_SUMMARY_END,
        ]
    )
    pattern = re.compile(
        rf"{re.escape(FEEDIAN_SUMMARY_START)}.*?{re.escape(FEEDIAN_SUMMARY_END)}",
        flags=re.DOTALL,
    )
    if pattern.search(note):
        return pattern.sub(block, note, count=1).strip()
    return f"{note.rstrip()}\n\n{block}".strip()


def merge_tags(*groups: list[str]) -> list[str]:
    seen: set[str] = set()
    merged: list[str] = []
    for group in groups:
        for raw in group:
            tag = normalize_tag(str(raw))
            if tag and tag not in seen:
                seen.add(tag)
                merged.append(tag)
    return merged


def add_fetch_diagnostics(frontmatter: dict[str, Any], page: PageFetchResult) -> None:
    if page.fetch_method:
        frontmatter["fetch_method"] = page.fetch_method
    if page.extraction_method:
        frontmatter["extraction_method"] = page.extraction_method
    if page.content_encoding:
        frontmatter["content_encoding"] = page.content_encoding
    if page.text:
        frontmatter["content_chars"] = len(page.text)
    if page.discussion_text:
        frontmatter["page_discussion_chars"] = len(page.discussion_text)
    if page.content_truncated:
        frontmatter["content_truncated"] = True


def normalize_tag(tag: str) -> str:
    tag = tag.strip().lstrip("#").lower()
    tag = re.sub(r"\s+", "-", tag)
    tag = re.sub(r"[^\w\-/\u3040-\u30ff\u3400-\u9fff]+", "", tag)
    tag = tag.strip("-/")
    return tag


def yaml_frontmatter(values: dict[str, Any]) -> str:
    lines: list[str] = []
    for key, value in values.items():
        if value is None or value == "":
            continue
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {quote_yaml_scalar(str(item))}")
        else:
            lines.append(f"{key}: {quote_yaml_scalar(str(value))}")
    return "\n".join(lines)


def quote_yaml_scalar(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def escape_markdown_heading(value: str) -> str:
    return value.replace("\n", " ").strip()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def debug_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)
