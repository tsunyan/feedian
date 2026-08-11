from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    replace_legacy: bool = False,
) -> RenderReport:
    if replace_legacy and not apply:
        raise ValueError("replace_legacy requires apply=True.")
    paths = vault_paths(vault_root)
    output_root = paths.root / config.raw_folder if apply else paths.state_dir / "staging" / config.raw_folder
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
    for row in rows:
        metadata = json.loads(str(row["metadata_json"]))
        provider = str(row["provider"])
        settings = config.providers.get(provider)
        if settings is None:
            continue
        destination = output_root / settings.folder
        filename = _note_filename(metadata, str(row["native_id"]))
        main_path = destination / filename
        comments_path = destination / comments_note_filename(filename)
        comments = _comments_for_resource(store, row["resource_id"])
        assets = _materialize_assets(
            store, row["resource_id"], row["resource_revision_id"], output_root / "assets", main_path.parent
        )
        main_document = _render_main_document(row, metadata, comments_path.stem if comments else None, assets)
        write_result = _write_generated(
            main_path,
            main_document,
            apply=apply,
            replace_legacy=replace_legacy,
            legacy_matches=_legacy_matches(store, paths.root, config.raw_folder, main_path),
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
                replace_legacy=replace_legacy,
                legacy_matches=_legacy_matches(store, paths.root, config.raw_folder, comments_path),
            )
            if comments_result == "written":
                comments_written += 1
            elif comments_result == "conflict":
                conflicts += 1
    return RenderReport(written, skipped, conflicts, comments_written, output_root)


def _note_filename(metadata: dict[str, Any], native_id: str) -> str:
    title = sanitize_filename(str(metadata.get("title") or metadata.get("link") or "Untitled"))
    return f"{(title or 'Untitled')[:60].rstrip(' .') or 'Untitled'} - {native_id}.md"


def _render_main_document(
    row: Any, metadata: dict[str, Any], comments_note: str | None, assets: list[tuple[str, str]]
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
    if assets:
        lines.extend(["## Images (Original)", ""])
        for relative_path, alt_text in assets:
            lines.extend([f"![{alt_text}]({relative_path})", ""])
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
        metadata_json = json.loads(str(comment["metadata_json"]))
        timestamp = metadata_json.get("timestamp")
        if timestamp:
            lines.extend([f"- Posted: {timestamp}", ""])
    return _with_render_hash("\n".join(lines).rstrip() + "\n")


def _comments_for_resource(store: VaultStore, resource_id: str | None) -> list[Any]:
    if not resource_id:
        return []
    return store.connection.execute(
        """
        SELECT c.author, cr.body, cr.tags_json, cr.star_count, cr.metadata_json
        FROM comment AS c
        JOIN comment_revision AS cr ON cr.comment_revision_id = c.current_revision_id
        WHERE c.resource_id = ? AND c.removed_at IS NULL
        ORDER BY cr.star_count DESC, cr.created_at ASC, c.author ASC
        """,
        (resource_id,),
    ).fetchall()


def _materialize_assets(
    store: VaultStore,
    resource_id: str | None,
    resource_revision_id: str | None,
    assets_dir: Path,
    note_directory: Path,
) -> list[tuple[str, str]]:
    if not resource_id or not resource_revision_id:
        return []
    rows = store.connection.execute(
        """
        SELECT p.content, p.sha256, p.media_type, a.alt_text
        FROM asset AS a
        JOIN payload AS p ON p.payload_id = a.payload_id
        WHERE a.resource_id = ? AND a.resource_revision_id = ?
        ORDER BY a.created_at, a.asset_id
        """,
        (resource_id, resource_revision_id),
    ).fetchall()
    references: list[tuple[str, str]] = []
    for row in rows:
        extension = _displayable_image_extension(str(row["media_type"]))
        if extension is None:
            continue
        path = assets_dir / f"{row['sha256']}{extension}"
        if not path.exists() or path.read_bytes() != row["content"]:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_bytes(row["content"])
            temporary.replace(path)
        relative_path = os.path.relpath(path, start=note_directory).replace("\\", "/")
        references.append((relative_path, str(row["alt_text"])))
    return references


def _displayable_image_extension(media_type: str) -> str | None:
    normalized = media_type.split(";", 1)[0].strip().lower()
    return {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/avif": ".avif",
    }.get(normalized)


def _with_render_hash(document_without_hash: str) -> str:
    digest = hashlib.sha256(document_without_hash.encode("utf-8")).hexdigest()
    return document_without_hash.replace("feedian_managed: true\n", f"feedian_managed: true\nrender_hash: {digest}\n", 1)


def _write_generated(
    path: Path,
    document: str,
    *,
    apply: bool,
    replace_legacy: bool = False,
    legacy_matches: bool = False,
) -> str:
    if path.exists() and apply:
        existing_bytes = path.read_bytes()
        try:
            existing = existing_bytes.decode("utf-8")
        except UnicodeDecodeError:
            existing = ""
        if not _is_unchanged_generated_document(existing) and not (replace_legacy and legacy_matches):
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


def _legacy_matches(store: VaultStore, vault_root: Path, raw_folder: str, path: Path) -> bool:
    if not path.exists():
        return False
    raw_root = vault_root / raw_folder
    try:
        relative_path = path.relative_to(raw_root).as_posix()
    except ValueError:
        return False
    return store.matches_legacy_artifact(relative_path, path.read_bytes())


def _is_unchanged_generated_document(document: str) -> bool:
    match = RENDER_HASH_PATTERN.search(document)
    if match is None:
        return False
    without_hash = RENDER_HASH_PATTERN.sub("", document, count=1)
    # The hash line is immediately after feedian_managed and therefore leaves one empty line when removed.
    without_hash = without_hash.replace("feedian_managed: true\n\n", "feedian_managed: true\n", 1)
    return hashlib.sha256(without_hash.encode("utf-8")).hexdigest() == match.group(1)
