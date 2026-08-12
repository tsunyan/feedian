from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .markdown import comments_note_filename, escape_markdown_heading, sanitize_filename, yaml_frontmatter
from .store import VaultStore
from .vault import VaultConfig, vault_paths


RENDER_HASH_PATTERN = re.compile(r"(?m)^render_hash: ([0-9a-f]{64})\r?$")


@dataclass(frozen=True)
class RenderReport:
    written: int = 0
    skipped: int = 0
    conflicts: int = 0
    comments_written: int = 0
    output_root: Path = Path(".")


def render_raw_views(
    store: VaultStore,
    vault_root: str | Path,
    config: VaultConfig,
    *,
    apply: bool = False,
    progress: Callable[[int, int], None] | None = None,
) -> RenderReport:
    paths = vault_paths(vault_root)
    output_root = paths.root / config.raw_folder if apply else paths.state_dir / "staging" / config.raw_folder
    total = int(
        store.connection.execute(
            """
            SELECT COUNT(*)
            FROM source_item AS s
            JOIN source_item_revision AS sr ON sr.source_revision_id = s.current_revision_id
            """
        ).fetchone()[0]
    )
    if progress is not None:
        progress(0, total)
    if not apply and output_root.exists():
        shutil.rmtree(output_root)
    rows = store.connection.execute(
        """
        SELECT s.source_item_id, s.provider, s.account, s.native_id, s.resource_id, s.removed_at,
               sr.metadata_json,
               rr.resource_revision_id, rr.title AS resource_title, rr.content_markdown, rr.discussion_text,
               rr.created_at AS resource_revision_created_at,
               (SELECT fc.warning FROM fetch_capture AS fc WHERE fc.resource_id = s.resource_id
                ORDER BY fc.fetched_at DESC LIMIT 1) AS fetch_warning,
               (SELECT fc.extracted_by FROM fetch_capture AS fc WHERE fc.resource_id = s.resource_id
                ORDER BY fc.fetched_at DESC LIMIT 1) AS extracted_by
        FROM source_item AS s
        JOIN source_item_revision AS sr ON sr.source_revision_id = s.current_revision_id
        LEFT JOIN resource AS r ON r.resource_id = s.resource_id
        LEFT JOIN resource_revision AS rr ON rr.resource_revision_id = r.current_revision_id
        ORDER BY s.provider, s.created_at, s.source_item_id
        """
    ).fetchall()
    written = skipped = conflicts = comments_written = 0
    for processed, row in enumerate(rows, start=1):
        metadata = json.loads(str(row["metadata_json"]))
        provider = str(row["provider"])
        settings = config.providers.get(provider)
        if settings is None:
            if progress is not None:
                progress(processed, total)
            continue
        destination = output_root / settings.folder
        filename = _note_filename(metadata, str(row["native_id"]))
        main_path = destination / filename
        if apply:
            _reconcile_legacy_generated_paths(destination, str(row["native_id"]), main_path)
        comments_path = destination / comments_note_filename(filename)
        comments = _comments_for_resource(store, row["resource_id"])
        images = _external_images(store, row["resource_id"])
        main_document = _render_main_document(row, metadata, comments_path.stem if comments else None, images)
        write_result = _write_generated(
            main_path,
            main_document,
            apply=apply,
        )
        if write_result == "written":
            written += 1
        elif write_result == "skipped":
            skipped += 1
        else:
            conflicts += 1
        if comments:
            comments_document = _render_comments_document(row, metadata, comments, filename)
            comments_result = _write_generated(
                comments_path,
                comments_document,
                apply=apply,
            )
            if comments_result == "written":
                comments_written += 1
            elif comments_result == "conflict":
                conflicts += 1
        if progress is not None:
            progress(processed, total)
    return RenderReport(written, skipped, conflicts, comments_written, output_root)


def _note_filename(metadata: dict[str, Any], native_id: str) -> str:
    title = sanitize_filename(str(metadata.get("title") or metadata.get("link") or "Untitled"))
    return f"{(title or 'Untitled')[:60].rstrip(' .') or 'Untitled'} - {native_id}.md"


def _render_main_document(
    row: Any, metadata: dict[str, Any], comments_note: str | None, images: list[tuple[str, str]]
) -> str:
    title = str(metadata.get("title") or row["resource_title"] or metadata.get("link") or "Untitled")
    tags = [str(value) for value in metadata.get("tags") or [] if str(value).strip()]
    frontmatter = {
        "feedian_managed": True,
        "feedian_kind": "raw",
        "source_item_id": row["source_item_id"],
        "resource_id": row["resource_id"],
        "source_type": row["provider"],
        "source_id": row["native_id"],
        "source_status": "removed" if row["removed_at"] else "active",
        "source": metadata.get("link") or "",
        "title": title,
        "tags": tags,
    }
    lines = ["---", yaml_frontmatter(frontmatter), "---", "", f"# {escape_markdown_heading(title)}", ""]
    lines.extend(
        [
            f"- Source: {metadata.get('link') or ''}",
            f"- Source type: {row['provider']}",
            f"- Source ID: {row['native_id']}",
            f"- Resource ID: {row['resource_id'] or ''}",
            "",
        ]
    )
    if tags:
        lines.extend(["## Tags", "", " ".join(f"#{tag}" for tag in tags), ""])
    comment = str(metadata.get("note") or "").strip()
    excerpt = str(metadata.get("excerpt") or "").strip()
    if comment or excerpt:
        lines.extend(["## Bookmark Metadata", ""])
        if comment:
            lines.extend(["### Comment (Original)", "", comment, ""])
        if excerpt:
            lines.extend(["### Excerpt (Original)", "", excerpt, ""])
    content = str(row["content_markdown"] or "").strip()
    if content:
        lines.extend(["## Content (Original)", "", content, ""])
    discussion = str(row["discussion_text"] or "").strip()
    if discussion:
        lines.extend(["## Reply (Original)", "", discussion, ""])
    if images:
        lines.extend(["## Images (Original)", ""])
        for source_url, alt_text in images:
            safe_alt = alt_text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
            safe_url = source_url.replace("<", "%3C").replace(">", "%3E").replace(" ", "%20")
            lines.extend([f"![{safe_alt}](<{safe_url}>)", ""])
    if comments_note:
        lines.extend(["## Hatena Comments", "", f"- [[{comments_note}]]", ""])
    warning = str(row["fetch_warning"] or "").strip()
    if warning:
        lines.extend(["## Fetch Warning", "", warning, ""])
    extracted_by = str(row["extracted_by"] or "").strip()
    if extracted_by:
        lines.extend(["## Extraction", "", f"- {extracted_by}", ""])
    return _with_render_hash("\n".join(lines).rstrip() + "\n")


def _render_comments_document(row: Any, metadata: dict[str, Any], comments: list[Any], main_filename: str) -> str:
    title = str(metadata.get("title") or metadata.get("link") or "Untitled")
    frontmatter = {
        "feedian_managed": True,
        "feedian_kind": "comments",
        "resource_id": row["resource_id"],
        "source": metadata.get("link") or "",
        "comment_count": len(comments),
    }
    lines = ["---", yaml_frontmatter(frontmatter), "---", "", f"# Comments: {escape_markdown_heading(title)}", ""]
    lines.extend([f"- Source: {metadata.get('link') or ''}", f"- Main note: [[{Path(main_filename).stem}]]", ""])
    for comment in comments:
        tags = json.loads(str(comment["tags_json"]))
        stars = comment["star_count"]
        heading = str(comment["author"])
        if stars is not None:
            heading += f" ★{stars}"
        lines.extend([f"## {escape_markdown_heading(heading)}", "", str(comment["body"]), ""])
        if tags:
            lines.extend([" ".join(f"#{tag}" for tag in tags), ""])
        timestamp = str(comment["posted_at"])
        if timestamp:
            lines.extend([f"- Posted: {timestamp}", ""])
    return _with_render_hash("\n".join(lines).rstrip() + "\n")


def _comments_for_resource(store: VaultStore, resource_id: str | None) -> list[Any]:
    if not resource_id:
        return []
    return store.connection.execute(
        """
        SELECT c.author, cr.body, cr.tags_json, cr.star_count, cr.posted_at
        FROM comment AS c
        JOIN comment_revision AS cr ON cr.comment_revision_id = c.current_revision_id
        WHERE c.resource_id = ? AND c.removed_at IS NULL
        ORDER BY COALESCE(cr.star_count, 0) DESC,
                 CASE WHEN cr.posted_at = '' THEN 1 ELSE 0 END,
                 cr.posted_at ASC, c.comment_id ASC
        """,
        (resource_id,),
    ).fetchall()


def _external_images(store: VaultStore, resource_id: str | None) -> list[tuple[str, str]]:
    if not resource_id:
        return []
    rows = store.connection.execute(
        """
        SELECT source_url, alt_text
        FROM resource_image
        WHERE resource_id = ?
        ORDER BY position, resource_image_id
        """,
        (resource_id,),
    ).fetchall()
    return [(str(row["source_url"]), str(row["alt_text"])) for row in rows]


def _with_render_hash(document_without_hash: str) -> str:
    digest = hashlib.sha256(document_without_hash.encode("utf-8")).hexdigest()
    return document_without_hash.replace("feedian_managed: true\n", f"feedian_managed: true\nrender_hash: {digest}\n", 1)


def _write_generated(
    path: Path,
    document: str,
    *,
    apply: bool,
) -> str:
    if path.exists() and apply:
        existing_bytes = path.read_bytes()
        try:
            existing = existing_bytes.decode("utf-8")
        except UnicodeDecodeError:
            existing = ""
        if not _is_unchanged_generated_document(existing) and not _is_legacy_generated_document(existing):
            return "conflict"
        if existing_bytes == document.encode("utf-8"):
            return "skipped"
    elif path.exists() and path.read_text(encoding="utf-8") == document:
        return "skipped"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(document, encoding="utf-8", newline="\n")
    temporary.replace(path)
    return "written"


def _is_unchanged_generated_document(document: str) -> bool:
    match = RENDER_HASH_PATTERN.search(document)
    if match is None:
        return False
    without_hash = RENDER_HASH_PATTERN.sub("", document, count=1)
    # The hash line is immediately after feedian_managed and therefore leaves one empty line when removed.
    without_hash = without_hash.replace("feedian_managed: true\n\n", "feedian_managed: true\n", 1)
    return hashlib.sha256(without_hash.encode("utf-8")).hexdigest() == match.group(1)


def _is_legacy_generated_document(document: str) -> bool:
    """Recognize pre-canonical Feedian raw views without treating ordinary Markdown as generated."""
    document = document.replace("\r\n", "\n").replace("\r", "\n")
    if not document.startswith("---\n"):
        return False
    frontmatter_end = document.find("\n---\n", 4)
    if frontmatter_end < 0:
        return False
    frontmatter = document[4:frontmatter_end]
    canonical_required = (
        re.search(r"(?m)^source_type: \"?(?:hatena|raindrop)\"?\s*$", frontmatter),
        re.search(r"(?m)^source_id: \"?(?:hatena|raindrop)-[^\s\"]+\"?\s*$", frontmatter),
        re.search(r"(?m)^content_key: \"?url:[0-9a-f]{64}\"?\s*$", frontmatter),
    )
    raindrop_required = (
        re.search(r"(?m)^source: \"?https?://.+\"?\s*$", frontmatter),
        re.search(r"(?m)^raindrop_id: \"?[0-9]+\"?\s*$", frontmatter),
        re.search(r"(?m)^raindrop_collection_id: \"?-?[0-9]+\"?\s*$", frontmatter),
        re.search(r"(?m)^summary_generated_at: \"?[^\n\"]+\"?\s*$", frontmatter),
        re.search(r"(?m)^summary_model: \"?[^\n\"]+\"?\s*$", frontmatter),
    )
    return all(canonical_required) or all(raindrop_required)


def _reconcile_legacy_generated_paths(destination: Path, native_id: str, expected_path: Path) -> None:
    """Move or remove an old title-based Feedian path for the same stable source ID."""
    if not destination.exists():
        return
    candidates = sorted(destination.glob(f"* - {native_id}.md"))
    for candidate in candidates:
        if candidate == expected_path or candidate.name.endswith(".comments.md"):
            continue
        try:
            document = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not _is_legacy_generated_document(document):
            continue
        expected_path.parent.mkdir(parents=True, exist_ok=True)
        if not expected_path.exists():
            candidate.replace(expected_path)
        else:
            candidate.unlink()
