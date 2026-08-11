from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any

from .extract import PageFetchResult


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
) -> str:
    title = summary.get("note_title") or item.get("title") or page.title or item.get("link") or "Untitled"
    all_tags = merge_tags(base_tags or [], item.get("tags") or [], summary.get("tags") or [])
    frontmatter = {
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
