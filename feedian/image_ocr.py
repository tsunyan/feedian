from __future__ import annotations

import hashlib
import math
import os
import re
import tempfile
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request

from .extract import build_safe_opener, validate_fetch_url
from .llm_backends import (
    BackendAudit,
    BackendError,
    BackendPolicyError,
    BackendOutputLimitError,
    BackendProtocolError,
    BackendRateLimitError,
    BackendTimeoutError,
    BackendUnavailableError,
    IMAGE_OCR_PROMPT_VERSION,
    IMAGE_OCR_SCHEMA_VERSION,
    LLMBackend,
)
from .markdown import utc_now
from .store import VaultStore, stable_json
from .vault import ImageOCRSettings, VaultConfig, fetch_policy, positive_int_setting


SVG_EXTRACTOR_VERSION = "svg-text-v1"
NAME_GATE_PATTERNS = (
    "@2x", "@3x", "logo", "icon", "avatar", "profile", "button", "btn", "banner",
    "badge", "sprite", "spacer", "blank", "emoji", "favicon",
)
URL_DENYLIST = (
    (re.compile(r"^https?://b\.hatena\.ne\.jp/entry/image/", re.I), "denylist:b.hatena.ne.jp/entry/image"),
    (re.compile(r"^https?://b\.hatena\.ne\.jp/bc/", re.I), "denylist:b.hatena.ne.jp/bc"),
    (re.compile(r"^https?://pbs\.twimg\.com/amplify_video_thumb/", re.I), "denylist:pbs.twimg.com/amplify_video_thumb"),
    (re.compile(r"^https?://pbs\.twimg\.com/ext_tw_video_thumb/", re.I), "denylist:pbs.twimg.com/ext_tw_video_thumb"),
    (re.compile(r"^https?://pbs\.twimg\.com/tweet_video_thumb/", re.I), "denylist:pbs.twimg.com/tweet_video_thumb"),
    (re.compile(r"^https?://pbs\.twimg\.com/card_img/", re.I), "denylist:pbs.twimg.com/card_img"),
    (re.compile(r"^https?://pbs\.twimg\.com/cards/", re.I), "denylist:pbs.twimg.com/cards"),
    (re.compile(r"^https?://pbs\.twimg\.com/media/", re.I), "denylist:pbs.twimg.com/media"),
)
SUPPORTED_RASTER_MIMES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp", "image/avif"})
EXCLUDED_IMAGE_MIMES = frozenset({"image/tiff", "image/bmp", "image/x-icon", "image/vnd.microsoft.icon"})


@dataclass(frozen=True)
class ImageFetchResult:
    status: str
    source_url: str
    media_type: str = ""
    temporary_path: Path | None = None
    image_sha256: str = ""
    width: int | None = None
    height: int | None = None
    reason: str = ""
    transient: bool = False


@dataclass(frozen=True)
class ImageAnalysisResult:
    status: str
    method: str | None = None
    image_kind: str | None = None
    ocr_text: str = ""
    ocr_truncated: bool = False
    ignored_reason: str | None = None
    warning: str | None = None
    audit: BackendAudit | None = None
    failure_kind: str | None = None
    transient: bool = False


@dataclass
class ImageEnrichmentReport:
    remaining_resources: int = 0
    resources: int = 0
    candidate_rows: int = 0
    fetch_urls: int = 0
    analysis_groups: int = 0
    completed: int = 0
    ignored: int = 0
    reused_existing: int = 0
    shared_groups: int = 0
    propagated_rows: int = 0
    propagated_resources: int = 0
    ocr_truncated: int = 0
    failed: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    priced_requests: int = 0
    unpriced: int = 0
    unmetered: int = 0
    llm_parallelism: int = 0
    historical_seconds_per_image: float | None = None
    expected_seconds: float | None = None
    ignored_reasons: Counter[str] = field(default_factory=Counter)
    failure_kinds: Counter[str] = field(default_factory=Counter)


def normalize_alt(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip()


def prefetch_ignored_reason(source_url: str) -> str | None:
    lowered = source_url.lower()
    for pattern, reason in URL_DENYLIST:
        if pattern.search(source_url):
            return reason
    if "@2x" in lowered:
        return "name_pattern:@2x"
    if "@3x" in lowered:
        return "name_pattern:@3x"
    tokens = set(re.findall(r"[a-z0-9]+", lowered))
    for pattern in NAME_GATE_PATTERNS[2:]:
        if pattern in tokens:
            return f"name_pattern:{pattern}"
    return None


def attempt_target(source_url: str, alt_text: str, backend: str, settings: ImageOCRSettings) -> str:
    payload = {
        "source_url": source_url,
        "alt_text": normalize_alt(alt_text),
        "backend": backend,
        "prompt_version": IMAGE_OCR_PROMPT_VERSION,
        "schema_version": IMAGE_OCR_SCHEMA_VERSION,
        "timeout_seconds": settings.timeout_seconds,
        "max_bytes": settings.max_bytes,
        "max_pixels": settings.max_pixels,
        "min_short_edge_pixels": settings.min_short_edge_pixels,
        "name_patterns": NAME_GATE_PATTERNS,
        "denylist": tuple((pattern.pattern, reason) for pattern, reason in URL_DENYLIST),
        "svg_extractor_version": SVG_EXTRACTOR_VERSION,
    }
    return hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()


def analysis_fingerprint(
    image_sha256: str, alt_text: str, backend: str, *, svg: bool = False,
) -> str:
    payload = {
        "image_sha256": image_sha256,
        "alt_text": normalize_alt(alt_text),
        "schema_version": IMAGE_OCR_SCHEMA_VERSION,
    }
    if svg:
        payload["extractor_version"] = SVG_EXTRACTOR_VERSION
    else:
        payload["backend"] = backend
        payload["prompt_version"] = IMAGE_OCR_PROMPT_VERSION
    return hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()


def _positive_svg_number(value: str | None) -> float | None:
    if not value:
        return None
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)(?:px)?\s*", value, re.I)
    if match is None:
        return None
    number = float(match.group(1))
    return number if math.isfinite(number) and number > 0 else None


def extract_svg_text(content: bytes, settings: ImageOCRSettings) -> ImageAnalysisResult:
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", content, re.I):
        return ImageAnalysisResult(status="failed", failure_kind="unsafe_svg", warning="DTD or entity declaration")
    try:
        root = ET.fromstring(content)
    except (ET.ParseError, ValueError) as exc:
        return ImageAnalysisResult(status="failed", failure_kind="invalid_svg", warning=str(exc)[:300])
    width = _positive_svg_number(root.attrib.get("width"))
    height = _positive_svg_number(root.attrib.get("height"))
    if width is None or height is None:
        viewbox = root.attrib.get("viewBox") or root.attrib.get("viewbox")
        if viewbox:
            try:
                values = [float(value) for value in re.split(r"[\s,]+", viewbox.strip())]
            except ValueError:
                values = []
            if len(values) == 4 and all(math.isfinite(value) for value in values):
                width, height = values[2], values[3]
    if width is None or height is None or width <= 0 or height <= 0:
        return ImageAnalysisResult(status="ignored", ignored_reason="svg_unknown_dimensions")
    if min(width, height) < settings.min_short_edge_pixels:
        return ImageAnalysisResult(status="ignored", ignored_reason="small_dimensions")
    parts: list[str] = []
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == "text":
            text = re.sub(r"\s+", " ", " ".join(element.itertext())).strip()
            if text:
                parts.append(text)
    text = " ".join(parts)
    if not text:
        return ImageAnalysisResult(status="ignored", ignored_reason="svg_without_text")
    truncated = len(text) > settings.max_ocr_chars_per_image
    return ImageAnalysisResult(
        status="completed", method="svg_text", image_kind="explanatory",
        ocr_text=text[:settings.max_ocr_chars_per_image], ocr_truncated=truncated,
    )


def raster_dimensions(media_type: str, data: bytes) -> tuple[int, int, bool] | None:
    if media_type == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big"), b"acTL" in data
    if media_type == "image/gif" and data[:6] in {b"GIF87a", b"GIF89a"} and len(data) >= 10:
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little"), data.count(b"\x2c") > 1
    if media_type == "image/jpeg" and data.startswith(b"\xff\xd8"):
        index = 2
        while index + 9 <= len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                return int.from_bytes(data[index + 7:index + 9], "big"), int.from_bytes(data[index + 5:index + 7], "big"), False
            if marker in {0xD8, 0xD9}:
                index += 2
                continue
            if index + 4 > len(data):
                break
            length = int.from_bytes(data[index + 2:index + 4], "big")
            if length < 2:
                break
            index += 2 + length
        return None
    if media_type == "image/webp" and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        if data[12:16] == b"VP8X" and len(data) >= 30:
            width = 1 + int.from_bytes(data[24:27], "little")
            height = 1 + int.from_bytes(data[27:30], "little")
            return width, height, bool(data[20] & 0x02) or b"ANIM" in data
        if data[12:16] == b"VP8L" and len(data) >= 25:
            bits = int.from_bytes(data[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1, False
        if data[12:16] == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
            return (
                int.from_bytes(data[26:28], "little") & 0x3FFF,
                int.from_bytes(data[28:30], "little") & 0x3FFF,
                False,
            )
    if media_type == "image/avif":
        index = data.find(b"ispe")
        if index >= 4 and index + 16 <= len(data):
            return int.from_bytes(data[index + 8:index + 12], "big"), int.from_bytes(data[index + 12:index + 16], "big"), False
    return None


def _read_request(
    opener: Any, request: Request, timeout: int, limit: int,
) -> tuple[bytes, str, int, bool, int | None]:
    with opener.open(request, timeout=timeout) as response:
        raw = response.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("max_bytes")
        media_type = str(response.headers.get("Content-Type", "")).split(";", 1)[0].strip().lower()
        status = int(getattr(response, "status", 200) or 200)
        length = response.headers.get("Content-Length")
        content_range = str(response.headers.get("Content-Range", ""))
        range_match = re.search(r"/(\d+)\s*$", content_range)
        declared_total = int(range_match.group(1)) if range_match else (
            int(length) if status == 200 and length and str(length).isdigit() else None
        )
        complete = status == 200 and (declared_total is None or declared_total <= len(raw))
        return raw, media_type, status, complete, declared_total


def _write_temporary_image(content: bytes, parent: Path, media_type: str) -> Path:
    suffix = {
        "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
        "image/webp": ".webp", "image/avif": ".avif", "image/svg+xml": ".svg",
    }.get(media_type, ".img")
    parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix="feedian-image-", suffix=suffix, dir=parent)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
    return Path(name)


def fetch_image(
    source_url: str, config: VaultConfig, temporary_parent: Path | None = None, *, _retry_429: bool = True,
) -> ImageFetchResult:
    settings = config.image_ocr
    network = fetch_policy(config).network
    try:
        validate_fetch_url(source_url, allowed_private_hosts=network.allowed_private_hosts)
        opener = build_safe_opener(network)
        headers = {"User-Agent": "feedian/0.1 (+https://github.com/) Python urllib", "Accept": "image/*", "Range": "bytes=0-8191"}
        initial, media_type, _, complete, declared_total = _read_request(
            opener, Request(source_url, headers=headers, method="GET"), settings.timeout_seconds, settings.max_bytes,
        )
        if declared_total is not None and declared_total > settings.max_bytes:
            return ImageFetchResult("failed", source_url, media_type, reason="max_bytes")
        if not media_type.startswith("image/"):
            return ImageFetchResult("ignored", source_url, media_type, reason="non_image_mime")
        if media_type in EXCLUDED_IMAGE_MIMES:
            return ImageFetchResult("ignored", source_url, media_type, reason=f"excluded_format:{media_type}")
        if media_type not in SUPPORTED_RASTER_MIMES and media_type != "image/svg+xml":
            return ImageFetchResult("failed", source_url, media_type, reason="unsupported_image_header")
        if media_type == "image/svg+xml":
            content = initial if complete else _read_request(
                opener, Request(source_url, headers={**headers, "Range": "bytes=0-"}, method="GET"),
                settings.timeout_seconds, settings.max_bytes,
            )[0]
            path = _write_temporary_image(content, temporary_parent or Path(tempfile.gettempdir()), media_type)
            return ImageFetchResult(
                "ready", source_url, media_type, path, hashlib.sha256(content).hexdigest(),
            )
        dimensions = raster_dimensions(media_type, initial)
        header = initial
        if dimensions is None and not complete:
            header, media_type2, _, complete, declared_total = _read_request(
                opener, Request(source_url, headers={**headers, "Range": "bytes=0-65535"}, method="GET"),
                settings.timeout_seconds, settings.max_bytes,
            )
            if declared_total is not None and declared_total > settings.max_bytes:
                return ImageFetchResult("failed", source_url, media_type, reason="max_bytes")
            if media_type2:
                media_type = media_type2
            dimensions = raster_dimensions(media_type, header)
        if dimensions is None:
            return ImageFetchResult("failed", source_url, media_type, reason="invalid_or_incomplete_header")
        width, height, animated = dimensions
        if animated:
            return ImageFetchResult("ignored", source_url, media_type, reason="animated")
        if width <= 0 or height <= 0 or width * height > settings.max_pixels:
            return ImageFetchResult("failed", source_url, media_type, reason="max_pixels")
        if min(width, height) < settings.min_short_edge_pixels:
            return ImageFetchResult("ignored", source_url, media_type, reason="small_dimensions", width=width, height=height)
        content = header if complete else _read_request(
            opener, Request(source_url, headers={**headers, "Range": "bytes=0-"}, method="GET"),
            settings.timeout_seconds, settings.max_bytes,
        )[0]
        final_dimensions = raster_dimensions(media_type, content)
        if final_dimensions is not None and final_dimensions[2]:
            return ImageFetchResult("ignored", source_url, media_type, reason="animated")
        path = _write_temporary_image(content, temporary_parent or Path(tempfile.gettempdir()), media_type)
        return ImageFetchResult(
            "ready", source_url, media_type, path, hashlib.sha256(content).hexdigest(), width, height,
        )
    except HTTPError as exc:
        if exc.code == 429 and _retry_429:
            retry_after = str(exc.headers.get("Retry-After", "") if exc.headers else "").strip()
            delay: float | None = float(retry_after) if retry_after.isdigit() else None
            if delay is None and retry_after:
                try:
                    retry_at = parsedate_to_datetime(retry_after)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=timezone.utc)
                    delay = max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
                except (TypeError, ValueError, OverflowError):
                    delay = None
            if delay is not None:
                time.sleep(min(60.0, delay))
                return fetch_image(source_url, config, temporary_parent, _retry_429=False)
        transient = exc.code == 429 or exc.code >= 500
        return ImageFetchResult("failed", source_url, reason=f"http_{exc.code}", transient=transient)
    except (URLError, TimeoutError) as exc:
        reason = "timeout" if isinstance(getattr(exc, "reason", exc), TimeoutError) else "network"
        return ImageFetchResult("failed", source_url, reason=reason, transient=True)
    except ValueError as exc:
        reason = str(exc)
        return ImageFetchResult("failed", source_url, reason=reason, transient=False)
    except Exception as exc:
        return ImageFetchResult("failed", source_url, reason=type(exc).__name__.lower(), transient=True)


def _due(row: Any, target: str, settings: ImageOCRSettings, force: bool) -> bool:
    if force or str(row["last_attempt_target"] or "") != target:
        return True
    if str(row["last_attempt_status"] or "") == "failed":
        failure = str(row["last_failure_kind"] or "")
        transient = failure.startswith("transient:")
        return transient and not bool(row["transient_retry_used"])
    if bool(row["ocr_truncated"]) and int(row["ocr_char_limit"] or 0) < settings.max_ocr_chars_per_image:
        return True
    if str(row["analysis_status"] or "pending") in {"completed", "ignored"}:
        return False
    return True


def _analysis_from_fetch(
    fetched: ImageFetchResult, *, backend: LLMBackend, model: str, alt_text: str,
    settings: ImageOCRSettings, temporary_parent: Path,
    backend_gate: threading.Semaphore,
) -> ImageAnalysisResult:
    if fetched.status == "ignored":
        return ImageAnalysisResult(status="ignored", ignored_reason=fetched.reason)
    if fetched.status == "failed":
        return ImageAnalysisResult(
            status="failed", failure_kind=fetched.reason, warning=fetched.reason, transient=fetched.transient,
        )
    if fetched.media_type == "image/svg+xml":
        if fetched.temporary_path is None:
            return ImageAnalysisResult(status="failed", failure_kind="missing_temporary_image")
        try:
            return extract_svg_text(fetched.temporary_path.read_bytes(), settings)
        except OSError as exc:
            return ImageAnalysisResult(
                status="failed", failure_kind="temporary_image_io", warning=str(exc)[:300], transient=True,
            )
    if fetched.temporary_path is None:
        return ImageAnalysisResult(status="failed", failure_kind="missing_temporary_image")
    try:
        with backend_gate:
            audit = backend.analyze_image(
                model=model, image_path=fetched.temporary_path, media_type=fetched.media_type,
                source_url=fetched.source_url,
                alt_text=alt_text, max_ocr_chars=settings.max_ocr_chars_per_image,
                timeout_seconds=settings.timeout_seconds, temporary_parent=temporary_parent,
            )
        result = audit.result
        kind = str(result["image_kind"])
        if kind != "explanatory":
            return ImageAnalysisResult(
                status="ignored", method="llm", image_kind=kind,
                ignored_reason=f"classification:{kind}", audit=audit,
            )
        return ImageAnalysisResult(
            status="completed", method="llm", image_kind=kind,
            ocr_text=str(result["ocr_text"]), ocr_truncated=bool(result["ocr_truncated"]), audit=audit,
        )
    except BackendOutputLimitError as exc:
        return ImageAnalysisResult(
            status="failed", failure_kind="output_limit", warning=str(exc)[:300], transient=False,
        )
    except (BackendTimeoutError, BackendRateLimitError, BackendUnavailableError, BackendProtocolError) as exc:
        return ImageAnalysisResult(
            status="failed", failure_kind=type(exc).__name__, warning=str(exc)[:300], transient=True,
        )
    except BackendError as exc:
        return ImageAnalysisResult(
            status="failed", failure_kind=type(exc).__name__, warning=str(exc)[:300], transient=False,
        )
    except Exception as exc:
        return ImageAnalysisResult(
            status="failed", failure_kind=type(exc).__name__, warning=str(exc)[:300], transient=True,
        )


def _attempt_values(
    result: ImageAnalysisResult, fetched: ImageFetchResult, target: str, fingerprint: str,
    backend_id: str, model: str, settings: ImageOCRSettings, run_id: str | None,
) -> dict[str, Any]:
    now = utc_now()
    common: dict[str, Any] = {
        "last_attempt_target": target,
        "last_attempt_fingerprint": fingerprint or None,
        "last_attempt_status": result.status,
        "last_failure_kind": (
            f"transient:{result.failure_kind}" if result.status == "failed" and result.transient
            else result.failure_kind
        ),
        "transient_retry_used": 0,
        "last_attempt_at": now,
        "analysis_warning": result.warning,
    }
    if result.status == "failed":
        return common
    common.update({
        "image_sha256": fetched.image_sha256 or None,
        "analysis_status": result.status,
        "analysis_method": result.method,
        "image_kind": result.image_kind,
        "ocr_text": result.ocr_text,
        "ocr_truncated": int(result.ocr_truncated),
        "ocr_char_limit": settings.max_ocr_chars_per_image if result.status == "completed" else None,
        "ignored_reason": result.ignored_reason,
        "ocr_llm_run_id": run_id,
        "analysis_input_fingerprint": fingerprint or None,
        "analysis_backend": backend_id if result.method == "llm" else None,
        "analysis_model": model if result.method == "llm" else None,
        "analyzed_at": now,
    })
    return common


def enrich_images(
    store: VaultStore, vault_root: str | Path, config: VaultConfig, *, limit: int | None,
    all_resources: bool, force: bool = False, dry_run: bool = False,
    backend_instance: LLMBackend | None = None,
    planning: Callable[[ImageEnrichmentReport], None] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> ImageEnrichmentReport:
    if (limit is None) == (not all_resources):
        raise ValueError("Exactly one of limit or all_resources is required.")
    if limit is not None:
        positive_int_setting("enrich_images.limit", limit)
    settings = config.image_ocr
    for name in ImageOCRSettings.__dataclass_fields__:
        positive_int_setting(f"image_ocr.{name}", getattr(settings, name))
    backend_id = config.llm.backend
    backend = backend_instance
    if backend is None:
        from .llm_backends import get_backend
        backend = get_backend(backend_id)
    rows = store.resource_images_for_enrichment()
    targets = {
        str(row["resource_image_id"]): attempt_target(
            str(row["source_url"]), str(row["alt_text"] or ""), backend_id, settings,
        )
        for row in rows
    }
    due_rows = [row for row in rows if _due(row, targets[str(row["resource_image_id"])], settings, force)]
    resource_order: list[str] = []
    for row in due_rows:
        resource_id = str(row["resource_id"])
        if resource_id not in resource_order:
            resource_order.append(resource_id)
    selected_resources = set(resource_order if all_resources else resource_order[:limit])
    selected_all = [row for row in rows if str(row["resource_id"]) in selected_resources]
    selected = [row for row in due_rows if str(row["resource_id"]) in selected_resources]
    selected_due_ids = {str(row["resource_image_id"]) for row in selected}
    report = ImageEnrichmentReport(
        remaining_resources=len(resource_order), resources=len(selected_resources),
        candidate_rows=len(selected_all),
        reused_existing=sum(
            1 for row in selected_all if str(row["resource_image_id"]) not in selected_due_ids
        ),
    )
    planned_fetch_urls = {
        str(row["source_url"]) for row in selected
        if prefetch_ignored_reason(str(row["source_url"])) is None
    }
    planned_groups = {
        (str(row["source_url"]), normalize_alt(str(row["alt_text"] or "")))
        for row in selected if prefetch_ignored_reason(str(row["source_url"])) is None
    }
    report.fetch_urls = len(planned_fetch_urls)
    report.analysis_groups = len(planned_groups)
    report.llm_parallelism = min(settings.workers, max(1, backend.capabilities.max_parallelism))
    history = store.connection.execute(
        """
        SELECT AVG(duration_ms) FROM llm_run
        WHERE operation = 'image-ocr' AND backend = ? AND status = 'completed'
          AND duration_ms IS NOT NULL
        """,
        (backend_id,),
    ).fetchone()[0]
    if history is not None:
        report.historical_seconds_per_image = float(history) / 1000
        report.expected_seconds = (
            report.analysis_groups * report.historical_seconds_per_image / report.llm_parallelism
        )
    if planning is not None:
        planning(report)
    if not selected or dry_run:
        return report
    if not backend.capabilities.image_analysis:
        raise BackendPolicyError(f"{backend_id} does not support image analysis.")
    image_preflight = getattr(backend, "preflight_image", backend.preflight)
    backend_metadata = image_preflight()
    all_groups: dict[tuple[str, str], list[Any]] = {}
    for row in rows:
        all_groups.setdefault((str(row["source_url"]), normalize_alt(str(row["alt_text"] or ""))), []).append(row)
    selected_groups: dict[tuple[str, str], list[Any]] = {}
    for row in selected:
        selected_groups.setdefault((str(row["source_url"]), normalize_alt(str(row["alt_text"] or ""))), []).append(row)
    touched_resources: set[str] = set()
    pending_by_resource: dict[str, set[tuple[str, str]]] = {
        resource_id: set() for resource_id in selected_resources
    }
    for key, group in selected_groups.items():
        for resource_id in {str(row["resource_id"]) for row in group}:
            pending_by_resource[resource_id].add(key)
    completed_resources = 0

    def finish_group(key: tuple[str, str], group: list[Any]) -> None:
        nonlocal completed_resources
        for resource_id in {str(row["resource_id"]) for row in group}:
            pending = pending_by_resource[resource_id]
            pending.discard(key)
            if not pending:
                completed_resources += 1
                if progress is not None:
                    progress(completed_resources, len(selected_resources))

    fetch_urls: set[str] = set()
    ready_groups: list[tuple[tuple[str, str], list[Any]]] = []
    for key, group in selected_groups.items():
        reason = prefetch_ignored_reason(key[0])
        if reason is None:
            fetch_urls.add(key[0])
            ready_groups.append((key, group))
            continue
        result = ImageAnalysisResult(status="ignored", ignored_reason=reason)
        fetched = ImageFetchResult(status="ignored", source_url=key[0], reason=reason)
        ids = [str(row["resource_image_id"]) for row in all_groups[key]]
        target = targets[str(group[0]["resource_image_id"])]
        store.apply_image_analysis(ids, _attempt_values(result, fetched, target, "", backend_id, config.llm.model, settings, None))
        report.ignored += len(ids)
        report.propagated_rows += len(ids)
        report.ignored_reasons[reason] += len(ids)
        touched_resources.update(str(row["resource_id"]) for row in all_groups[key])
        finish_group(key, group)

    report.fetch_urls = len(fetch_urls)
    temporary_parent = Path(vault_root) / ".feedian" / "tmp"
    temporary_parent.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=settings.workers) as executor:
        fetch_futures = {
            executor.submit(fetch_image, url, config, temporary_parent): url for url in fetch_urls
        }
        fetched_by_url = {fetch_futures[future]: future.result() for future in as_completed(fetch_futures)}

    report.analysis_groups = sum(
        1 for key, _ in ready_groups if fetched_by_url[key[0]].status == "ready"
    )
    report.shared_groups = sum(1 for key, _ in ready_groups if len(all_groups[key]) > 1)
    effective_parallelism = report.llm_parallelism
    backend_gate = threading.Semaphore(effective_parallelism)
    with ThreadPoolExecutor(max_workers=settings.workers) as executor:
        futures: dict[Any, tuple[tuple[str, str], list[Any], str | None, float]] = {}
        for key, group in ready_groups:
            fetched = fetched_by_url[key[0]]
            fingerprint = analysis_fingerprint(
                fetched.image_sha256, key[1], backend_id,
                svg=fetched.media_type == "image/svg+xml",
            ) if fetched.image_sha256 else ""
            run_id: str | None = None
            if fetched.status == "ready" and fetched.media_type != "image/svg+xml":
                representative = group[0]
                run_id = store.start_llm_run(
                    resource_id=str(representative["resource_id"]),
                    resource_revision_id=str(representative["resource_revision_id"]),
                    operation="image-ocr", backend=backend_id, model=config.llm.model,
                    prompt_version=IMAGE_OCR_PROMPT_VERSION,
                    summary_schema_version=IMAGE_OCR_SCHEMA_VERSION,
                    input_fingerprint=fingerprint,
                    request={"source_url": key[0], "image_sha256": fetched.image_sha256,
                             "alt_text": key[1], "prompt_version": IMAGE_OCR_PROMPT_VERSION,
                             "schema_version": IMAGE_OCR_SCHEMA_VERSION},
                    auth_mode=backend.capabilities.auth_mode,
                    billing_mode=backend.capabilities.billing_mode,
                    backend_metadata=backend_metadata,
                )
            future = executor.submit(
                _analysis_from_fetch, fetched_by_url[key[0]], backend=backend, model=config.llm.model,
                alt_text=key[1], settings=settings, temporary_parent=temporary_parent,
                backend_gate=backend_gate,
            )
            futures[future] = (key, group, run_id, time.monotonic())
        for future in as_completed(futures):
            key, selected_group, run_id, started_at = futures[future]
            fetched = fetched_by_url[key[0]]
            result = future.result()
            fingerprint = analysis_fingerprint(
                fetched.image_sha256, key[1], backend_id, svg=fetched.media_type == "image/svg+xml",
            ) if fetched.image_sha256 else ""
            duration_ms = round((time.monotonic() - started_at) * 1000)
            if run_id is not None and result.audit is not None:
                store.finish_llm_run(
                    run_id, request=result.audit.request, response=result.audit.response,
                    result=result.audit.result, usage=result.audit.usage,
                    auth_mode=result.audit.auth_mode, billing_mode=result.audit.billing_mode,
                    backend_metadata=result.audit.metadata, duration_ms=duration_ms,
                )
                report.input_tokens += int(result.audit.usage.get("input_tokens", 0))
                report.output_tokens += int(result.audit.usage.get("output_tokens", 0))
                actual_cost = result.audit.response.get("total_cost_usd")
                if not isinstance(actual_cost, (int, float)) or isinstance(actual_cost, bool):
                    actual_cost = result.audit.metadata.get("cli_estimated_cost_usd")
                if isinstance(actual_cost, (int, float)) and not isinstance(actual_cost, bool) and actual_cost >= 0:
                    report.cost_usd += float(actual_cost)
                    report.priced_requests += 1
                elif result.audit.billing_mode == "metered-api":
                    report.unpriced += 1
                else:
                    report.unmetered += 1
            elif run_id is not None:
                store.finish_llm_run(
                    run_id, error=result.warning or result.failure_kind or "image analysis failed",
                    duration_ms=duration_ms,
                )
                if backend.capabilities.billing_mode == "metered-api":
                    report.unpriced += 1
                else:
                    report.unmetered += 1
            ids = [str(row["resource_image_id"]) for row in all_groups[key]]
            target = targets[str(selected_group[0]["resource_image_id"])]
            values = _attempt_values(result, fetched, target, fingerprint, backend_id, config.llm.model, settings, run_id)
            if result.status == "failed":
                # Do not destroy a previously adopted result. A confirmed byte
                # change makes it pending so ingest cannot consume stale OCR.
                for row in all_groups[key]:
                    row_values = dict(values)
                    if result.transient:
                        same_failed_target = (
                            str(row["last_attempt_target"] or "") == target
                            and str(row["last_failure_kind"] or "").startswith("transient:")
                        )
                        row_values["transient_retry_used"] = int(same_failed_target)
                    if fetched.image_sha256 and row["image_sha256"] and fetched.image_sha256 != row["image_sha256"]:
                        row_values["analysis_status"] = "pending"
                    elif str(row["analysis_status"] or "pending") not in {"completed", "ignored"}:
                        row_values["analysis_status"] = "failed"
                    store.apply_image_analysis([str(row["resource_image_id"])], row_values)
            else:
                store.apply_image_analysis(ids, values)
            count = len(ids)
            report.propagated_rows += count
            touched_resources.update(str(row["resource_id"]) for row in all_groups[key])
            if result.status == "completed":
                report.completed += count
                report.ocr_truncated += count if result.ocr_truncated else 0
            elif result.status == "ignored":
                report.ignored += count
                report.ignored_reasons[result.ignored_reason or "unknown"] += count
            else:
                report.failed += count
                report.failure_kinds[result.failure_kind or "unknown"] += count
            finish_group(key, selected_group)
    for fetched in fetched_by_url.values():
        if fetched.temporary_path is not None:
            fetched.temporary_path.unlink(missing_ok=True)
    report.propagated_resources = len(touched_resources)
    return report


def report_line(report: ImageEnrichmentReport) -> str:
    cost = f"{report.cost_usd:.6f}" if report.priced_requests else "unknown"
    return (
        f"resources={report.resources} candidate_rows={report.candidate_rows} fetch_urls={report.fetch_urls} "
        f"analysis_groups={report.analysis_groups} completed={report.completed} ignored={report.ignored} "
        f"reused_existing={report.reused_existing} shared_groups={report.shared_groups} "
        f"propagated_rows={report.propagated_rows} propagated_resources={report.propagated_resources} "
        f"ocr_truncated={report.ocr_truncated} failed={report.failed} input_tokens={report.input_tokens} "
        f"output_tokens={report.output_tokens} cost_usd={cost} unpriced={report.unpriced} "
        f"unmetered={report.unmetered} llm_parallelism={report.llm_parallelism}"
    )
