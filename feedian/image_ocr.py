from __future__ import annotations

import hashlib
import io
import math
import os
import re
import tempfile
import threading
import time
import unicodedata
import warnings
import xml.etree.ElementTree as ET
from collections import Counter, deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import InvalidURL
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import Request

from PIL import Image, ImageOps, UnidentifiedImageError, __version__ as PILLOW_VERSION, features

from .extract import UnresolvableHostError, build_safe_opener, validate_fetch_url
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
IMAGE_ANALYSIS_TIMEOUT_SECONDS = 60
LEGACY_NAME_GATE_PATTERNS = (
    "@2x", "@3x", "logo", "icon", "avatar", "profile", "button", "btn", "banner",
    "badge", "sprite", "spacer", "blank", "emoji", "favicon",
)
LEGACY_URL_DENYLIST = (
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
MANDATORY_RASTER_MIMES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
_PILLOW_DECODE_LOCK = threading.Lock()


@dataclass(frozen=True)
class ImageFetchResult:
    status: str
    source_url: str
    media_type: str = ""
    temporary_path: Path | None = None
    image_sha256: str = ""
    width: int | None = None
    height: int | None = None
    oriented_width: int | None = None
    oriented_height: int | None = None
    sent_width: int | None = None
    sent_height: int | None = None
    sent_media_type: str = ""
    download_bytes: int | None = None
    resized: bool = False
    short_edge_floor_applied: bool = False
    reason: str = ""
    transient: bool = False
    warning: str | None = None


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
    remaining_resources_before: int = 0
    remaining_resources_after: int = 0
    selected_resources: int = 0
    candidate_rows: int = 0
    prefetch_ignored_rows: int = 0
    planned_fetch_urls: int = 0
    planned_analysis_groups: int = 0
    reused_existing_rows: int = 0
    fetched_urls: int = 0
    llm_requests: int = 0
    llm_explanatory_groups: int = 0
    llm_ignored_groups: int = 0
    svg_completed_groups: int = 0
    svg_ignored_groups: int = 0
    gate_ignored_rows: int = 0
    postfetch_ignored_rows: int = 0
    terminal_unavailable_rows: int = 0
    retained_current_rows: int = 0
    transient_failed_rows: int = 0
    propagated_rows: int = 0
    propagated_resources: int = 0
    ocr_truncated: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    priced_requests: int = 0
    unpriced: int = 0
    unmetered: int = 0
    llm_parallelism: int = 0
    historical_seconds_per_request: float | None = None
    expected_seconds: float | None = None
    input_tokens_per_request_avg: float | None = None
    input_tokens_per_request_p50: int | None = None
    input_tokens_per_request_p95: int | None = None
    input_tokens_per_request_max: int | None = None
    ignored_reasons: Counter[str] = field(default_factory=Counter)
    failure_kinds: Counter[str] = field(default_factory=Counter)


def normalize_alt(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip()


def gate_decision(source_url: str, settings: ImageOCRSettings) -> str:
    try:
        parsed = urlsplit(source_url)
        hostname = (parsed.hostname or "").lower()
    except ValueError:
        # A stored URL urlsplit cannot parse -- an unmatched bracket makes it
        # raise "Invalid IPv6 URL". This runs for every row while the plan is
        # built, long before any fetch, so raising here would abort the whole
        # run over one bad row. Let it pass the gate: fetch_image classifies it
        # as a terminal unavailable and the run keeps going.
        return "pass"
    decoded_path = unquote(parsed.path).lstrip("/")
    host_path = f"{hostname}/{decoded_path}"
    for prefix in settings.ignore_url_prefixes:
        if host_path.startswith(prefix):
            return f"url_prefix:{prefix}"
    tokens = set(re.findall(r"[a-z0-9]+", decoded_path.lower()))
    for token in settings.ignore_name_tokens:
        if token in tokens:
            return f"name_token:{token}"
    return "pass"


def prefetch_ignored_reason(
    source_url: str, settings: ImageOCRSettings | None = None,
) -> str | None:
    decision = gate_decision(source_url, settings or ImageOCRSettings())
    return None if decision == "pass" else decision


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
        "gate": gate_decision(source_url, settings),
        "svg_extractor_version": SVG_EXTRACTOR_VERSION,
    }
    return hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()


def _legacy_attempt_target(
    source_url: str, alt_text: str, backend: str, settings: ImageOCRSettings,
) -> str:
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
        "name_patterns": LEGACY_NAME_GATE_PATTERNS,
        "denylist": tuple((pattern.pattern, reason) for pattern, reason in LEGACY_URL_DENYLIST),
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


def _gif_is_animated(data: bytes) -> bool:
    """Count real image blocks without mistaking palette or compressed bytes for separators."""

    if len(data) < 13:
        return False
    index = 13
    packed = data[10]
    if packed & 0x80:
        index += 3 * (2 ** ((packed & 0x07) + 1))
    frames = 0
    while index < len(data):
        block = data[index]
        if block == 0x3B:  # trailer
            return False
        if block == 0x21:  # extension followed by data sub-blocks
            if index + 2 > len(data):
                return False
            index += 2
        elif block == 0x2C:  # image descriptor
            frames += 1
            if frames > 1:
                return True
            if index + 10 > len(data):
                return False
            descriptor_packed = data[index + 9]
            index += 10
            if descriptor_packed & 0x80:
                index += 3 * (2 ** ((descriptor_packed & 0x07) + 1))
            if index >= len(data):
                return False
            index += 1  # LZW minimum code size
        elif block == 0x00:  # padding seen in otherwise valid files
            index += 1
            continue
        else:
            return False
        while index < len(data):
            size = data[index]
            index += 1
            if size == 0:
                break
            if index + size > len(data):
                return False
            index += size
    return False


def raster_dimensions(media_type: str, data: bytes) -> tuple[int, int, bool] | None:
    if media_type == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big"), b"acTL" in data
    if media_type == "image/gif" and data[:6] in {b"GIF87a", b"GIF89a"} and len(data) >= 10:
        return (
            int.from_bytes(data[6:8], "little"),
            int.from_bytes(data[8:10], "little"),
            _gif_is_animated(data),
        )
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


def preflight_image_decoders() -> dict[str, Any]:
    """Fail before network access when a required raster decoder is unavailable."""

    Image.init()
    registered = set(Image.MIME.values())
    missing = sorted(
        media_type for media_type in MANDATORY_RASTER_MIMES - {"image/webp"}
        if media_type not in registered
    )
    try:
        webp_available = bool(features.check_module("webp"))
    except (ValueError, AttributeError):
        webp_available = False
    if not webp_available:
        missing.append("image/webp")
    if missing:
        raise BackendPolicyError(
            "Required Pillow image decoder(s) unavailable: " + ", ".join(sorted(set(missing)))
        )
    return {"pillow_version": PILLOW_VERSION, "webp": True}


def _pillow_supports(media_type: str) -> bool:
    Image.init()
    return media_type in set(Image.MIME.values())


def _save_normalized_png(image: Image.Image, parent: Path) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix="feedian-image-", suffix=".png", dir=parent)
    os.close(descriptor)
    path = Path(name)
    try:
        normalized = image
        if image.mode not in {"1", "L", "LA", "P", "RGB", "RGBA"}:
            normalized = image.convert("RGBA" if "A" in image.getbands() else "RGB")
        normalized.save(path, format="PNG")
        return path
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _prepare_raster(
    content: bytes,
    *,
    source_url: str,
    media_type: str,
    width: int,
    height: int,
    settings: ImageOCRSettings,
    temporary_parent: Path,
) -> ImageFetchResult:
    image_sha256 = hashlib.sha256(content).hexdigest()
    common = {
        "source_url": source_url,
        "media_type": media_type,
        "image_sha256": image_sha256,
        "width": width,
        "height": height,
        "download_bytes": len(content),
    }
    if media_type == "image/avif" and not _pillow_supports(media_type):
        path = _write_temporary_image(content, temporary_parent, media_type)
        return ImageFetchResult(
            status="ready", temporary_path=path, oriented_width=width, oriented_height=height,
            sent_width=width, sent_height=height, sent_media_type=media_type, **common,
        )

    try:
        with _PILLOW_DECODE_LOCK:
            previous_limit = Image.MAX_IMAGE_PIXELS
            try:
                Image.MAX_IMAGE_PIXELS = settings.max_pixels
                with warnings.catch_warnings():
                    warnings.simplefilter("error", Image.DecompressionBombWarning)
                    opened = Image.open(io.BytesIO(content))
                    decoded_width, decoded_height = opened.size
            finally:
                Image.MAX_IMAGE_PIXELS = previous_limit
        with opened:
            if (
                decoded_width <= 0 or decoded_height <= 0
                or decoded_width * decoded_height > settings.max_pixels
            ):
                return ImageFetchResult(status="failed", reason="max_pixels", **common)
            if bool(getattr(opened, "is_animated", False)):
                return ImageFetchResult(status="ignored", reason="animated", **common)
            opened.load()
            oriented = ImageOps.exif_transpose(opened)
            oriented_width, oriented_height = oriented.size
            if oriented_width * oriented_height > settings.max_pixels:
                return ImageFetchResult(status="failed", reason="max_pixels", **common)

            long_edge = max(oriented_width, oriented_height)
            short_edge = min(oriented_width, oriented_height)
            long_scale = settings.max_long_edge_pixels / long_edge
            short_scale = (settings.max_long_edge_pixels / 2) / short_edge
            scale = min(1.0, max(long_scale, short_scale))
            if scale < 1.0:
                sent_width = max(1, round(oriented_width * scale))
                sent_height = max(1, round(oriented_height * scale))
                resized = oriented.resize(
                    (sent_width, sent_height), resample=Image.Resampling.LANCZOS,
                )
            else:
                sent_width = oriented_width
                sent_height = oriented_height
                resized = None
    except (Image.DecompressionBombWarning, Image.DecompressionBombError):
        return ImageFetchResult(status="failed", reason="max_pixels", **common)
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        return ImageFetchResult(status="failed", reason="unsupported_image_decoder", **common)

    if resized is None:
        path = _write_temporary_image(content, temporary_parent, media_type)
        return ImageFetchResult(
            status="ready", temporary_path=path,
            oriented_width=oriented_width, oriented_height=oriented_height,
            sent_width=sent_width, sent_height=sent_height,
            sent_media_type=media_type, short_edge_floor_applied=False,
            **common,
        )

    try:
        path = _save_normalized_png(resized, temporary_parent)
    finally:
        resized.close()
    return ImageFetchResult(
        status="ready", temporary_path=path,
        oriented_width=oriented_width, oriented_height=oriented_height,
        sent_width=sent_width, sent_height=sent_height,
        sent_media_type="image/png", resized=True,
        short_edge_floor_applied=short_scale > long_scale,
        **common,
    )


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
        return _prepare_raster(
            content, source_url=source_url, media_type=media_type, width=width, height=height,
            settings=settings, temporary_parent=temporary_parent or Path(tempfile.gettempdir()),
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
    except UnresolvableHostError:
        return ImageFetchResult("failed", source_url, reason="dns", transient=True)
    except (URLError, TimeoutError) as exc:
        reason = "timeout" if isinstance(getattr(exc, "reason", exc), TimeoutError) else "network"
        return ImageFetchResult("failed", source_url, reason=reason, transient=True)
    except ValueError as exc:
        warning = str(exc)[:300]
        if warning == "max_bytes":
            reason = "max_bytes"
        elif any(marker in warning for marker in (
            "only http and https", "does not include a hostname", "non-public address",
        )):
            reason = "blocked_url"
        else:
            reason = "invalid_fetch_response"
        return ImageFetchResult(
            "failed", source_url, reason=reason, transient=False, warning=warning,
        )
    except OSError as exc:
        return ImageFetchResult(
            "failed", source_url, reason="temporary_image_io", transient=True,
            warning=str(exc)[:300],
        )
    except InvalidURL as exc:
        # A URL http.client cannot put on a request line -- an unencoded space or
        # control character in the path -- never becomes valid on a later run. The
        # generic handler below would file it as transient and pay one more fetch.
        return ImageFetchResult(
            "failed", source_url, reason="blocked_url", transient=False,
            warning=str(exc)[:300],
        )
    except Exception as exc:
        return ImageFetchResult("failed", source_url, reason=type(exc).__name__.lower(), transient=True)


def _due(
    row: Any, target: str, settings: ImageOCRSettings, force: bool, *, gate: str = "pass",
) -> bool:
    same_target = str(row["last_attempt_target"] or "") == target
    if gate != "pass":
        return not (same_target and str(row["last_attempt_status"] or "") == "ignored")
    if force or not same_target:
        return True
    if str(row["last_attempt_status"] or "") == "failed":
        failure = str(row["last_failure_kind"] or "")
        transient = failure.startswith("transient:")
        return transient and not bool(row["transient_retry_used"])
    if bool(row["ocr_truncated"]) and int(row["ocr_char_limit"] or 0) < settings.max_ocr_chars_per_image:
        return True
    if str(row["last_attempt_status"] or "") == "ignored":
        return False
    if str(row["analysis_status"] or "pending") in {"completed", "ignored"}:
        return False
    return True


def _retained_completed_gate(row: Any) -> bool:
    return (
        str(row["analysis_status"] or "") == "ignored"
        and str(row["image_kind"] or "") == "explanatory"
        and bool(row["analysis_method"])
        and bool(row["analysis_input_fingerprint"])
    )


def _retained_gate_result(row: Any, *, target: str) -> bool:
    return (
        str(row["analysis_status"] or "") == "ignored"
        and _has_adopted_result(row)
        and str(row["last_attempt_status"] or "") == "ignored"
        and not row["last_attempt_fingerprint"]
        and not row["last_failure_kind"]
        and str(row["last_attempt_target"] or "") != target
    )


def _has_adopted_result(row: Any) -> bool:
    status = str(row["analysis_status"] or "pending")
    if status == "completed":
        return True
    return status == "ignored" and bool(row["analysis_method"]) and bool(row["analysis_input_fingerprint"])


def _has_retained_payload(row: Any) -> bool:
    return bool(row["analysis_method"]) and bool(row["analysis_input_fingerprint"])


def _terminal_reason(reason: str) -> str:
    if reason in {"max_bytes", "max_pixels"}:
        return f"resource_limit:{reason}"
    return f"unavailable:{reason}"


def _analysis_from_fetch(
    fetched: ImageFetchResult, *, backend: LLMBackend, model: str, alt_text: str,
    settings: ImageOCRSettings, temporary_parent: Path,
) -> ImageAnalysisResult:
    if fetched.status == "ignored":
        return ImageAnalysisResult(status="ignored", ignored_reason=fetched.reason)
    if fetched.status == "failed":
        return ImageAnalysisResult(
            status="failed", failure_kind=fetched.reason,
            warning=fetched.warning or fetched.reason, transient=fetched.transient,
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
        audit = backend.analyze_image(
            model=model, image_path=fetched.temporary_path,
            media_type=fetched.sent_media_type or fetched.media_type,
            source_url=fetched.source_url,
            alt_text=alt_text, max_ocr_chars=settings.max_ocr_chars_per_image,
            timeout_seconds=IMAGE_ANALYSIS_TIMEOUT_SECONDS, temporary_parent=temporary_parent,
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


def _timed_analysis_from_fetch(
    fetched: ImageFetchResult, *, backend: LLMBackend, model: str, alt_text: str,
    settings: ImageOCRSettings, temporary_parent: Path,
) -> tuple[ImageAnalysisResult, int]:
    started_at = time.monotonic()
    result = _analysis_from_fetch(
        fetched, backend=backend, model=model, alt_text=alt_text,
        settings=settings, temporary_parent=temporary_parent,
    )
    return result, round((time.monotonic() - started_at) * 1000)


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


def _attempt_only_values(
    *, target: str, status: str, fingerprint: str = "", failure_kind: str | None = None,
    warning: str | None = None,
) -> dict[str, Any]:
    return {
        "last_attempt_target": target,
        "last_attempt_fingerprint": fingerprint or None,
        "last_attempt_status": status,
        "last_failure_kind": failure_kind,
        "transient_retry_used": 0,
        "last_attempt_at": utc_now(),
        "analysis_warning": warning,
    }


def _gate_values(
    row: Any, *, reason: str, target: str, backend_id: str, model: str,
    settings: ImageOCRSettings,
) -> tuple[dict[str, Any], bool]:
    attempt = _attempt_only_values(target=target, status="ignored")
    status = str(row["analysis_status"] or "pending")
    if _has_adopted_result(row):
        if status == "completed":
            return {**attempt, "analysis_status": "ignored", "ignored_reason": reason}, True
        return attempt, True
    if status == "pending" and _has_retained_payload(row):
        return attempt, True
    ignored = ImageAnalysisResult(status="ignored", ignored_reason=reason)
    fetched = ImageFetchResult(status="ignored", source_url=str(row["source_url"]), reason=reason)
    return _attempt_values(
        ignored, fetched, target, "", backend_id, model, settings, None,
    ), False


def _restore_gate_values(row: Any, *, target: str) -> dict[str, Any]:
    values = _attempt_only_values(
        target=target, status="completed",
        fingerprint=str(row["analysis_input_fingerprint"] or ""),
    )
    values.update({"analysis_status": "completed", "ignored_reason": None})
    return values


def _adopt_target_values(row: Any, *, target: str) -> dict[str, Any]:
    status = str(row["analysis_status"] or "ignored")
    return _attempt_only_values(
        target=target, status=status,
        fingerprint=str(row["analysis_input_fingerprint"] or ""),
    )


def _terminal_values(
    row: Any, *, fetched: ImageFetchResult, target: str, backend_id: str, model: str,
    settings: ImageOCRSettings, fingerprint: str,
) -> tuple[dict[str, Any], bool]:
    reason = _terminal_reason(fetched.reason)
    attempt = _attempt_only_values(
        target=target, status="ignored", fingerprint=fingerprint,
        failure_kind=reason, warning=fetched.warning or reason,
    )
    adopted = _has_adopted_result(row)
    changed = bool(
        adopted and fetched.image_sha256 and row["image_sha256"]
        and fetched.image_sha256 != row["image_sha256"]
    )
    if adopted:
        if changed:
            attempt["analysis_status"] = "pending"
        return attempt, True
    if str(row["analysis_status"] or "pending") == "pending" and _has_retained_payload(row):
        return attempt, True
    ignored = ImageAnalysisResult(
        status="ignored", ignored_reason=reason, failure_kind=reason, warning=reason,
    )
    values = _attempt_values(
        ignored, fetched, target, fingerprint, backend_id, model, settings, None,
    )
    values["last_failure_kind"] = reason
    return values, False


def _row_action(
    row: Any, *, target: str, legacy_target: str, gate: str,
    settings: ImageOCRSettings, force: bool,
) -> str:
    if gate != "pass":
        return "gate" if _due(row, target, settings, force, gate=gate) else "none"
    if force:
        return "fetch"
    if _retained_gate_result(row, target=target):
        return "restore" if _retained_completed_gate(row) else "adopt"
    if (
        str(row["last_attempt_target"] or "") == legacy_target
        and (
            str(row["analysis_status"] or "") == "completed"
            or (
                str(row["analysis_status"] or "") == "ignored"
                and str(row["analysis_method"] or "") in {"llm", "svg_text"}
            )
        )
    ):
        return "adopt"
    return "fetch" if _due(row, target, settings, False, gate=gate) else "none"


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
    for name in (
        "workers", "timeout_seconds", "max_bytes", "max_pixels", "min_short_edge_pixels",
        "max_long_edge_pixels", "max_ocr_chars_per_image", "max_ocr_images_per_resource",
        "max_ocr_chars_per_resource",
    ):
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
    legacy_targets = {
        str(row["resource_image_id"]): _legacy_attempt_target(
            str(row["source_url"]), str(row["alt_text"] or ""), backend_id, settings,
        )
        for row in rows
    }
    gates = {
        str(row["resource_image_id"]): gate_decision(str(row["source_url"]), settings)
        for row in rows
    }
    actions = {
        str(row["resource_image_id"]): _row_action(
            row, target=targets[str(row["resource_image_id"])],
            legacy_target=legacy_targets[str(row["resource_image_id"])],
            gate=gates[str(row["resource_image_id"])], settings=settings, force=force,
        )
        for row in rows
    }
    due_rows = [row for row in rows if actions[str(row["resource_image_id"])] != "none"]
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
        remaining_resources_before=len(resource_order),
        remaining_resources_after=len(resource_order),
        selected_resources=len(selected_resources),
        candidate_rows=len(selected_all),
        reused_existing_rows=sum(
            1 for row in selected_all if str(row["resource_image_id"]) not in selected_due_ids
        ),
    )
    planned_fetch_urls = {
        str(row["source_url"]) for row in selected
        if actions[str(row["resource_image_id"])] == "fetch"
    }
    planned_groups = {
        (str(row["source_url"]), normalize_alt(str(row["alt_text"] or "")))
        for row in selected if actions[str(row["resource_image_id"])] == "fetch"
    }
    report.prefetch_ignored_rows = sum(
        1 for row in selected if actions[str(row["resource_image_id"])] == "gate"
    )
    report.planned_fetch_urls = len(planned_fetch_urls)
    report.planned_analysis_groups = len(planned_groups)
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
        report.historical_seconds_per_request = float(history) / 1000
        report.expected_seconds = (
            report.planned_analysis_groups * report.historical_seconds_per_request / report.llm_parallelism
        )
    for row in selected:
        image_id = str(row["resource_image_id"])
        if actions[image_id] == "gate":
            report.ignored_reasons[gates[image_id]] += 1
    if planning is not None:
        planning(report)
    if not selected or dry_run:
        return report
    if planned_fetch_urls:
        if not backend.capabilities.image_analysis:
            raise BackendPolicyError(f"{backend_id} does not support image analysis.")
        if not backend.supports_model(config.llm.model):
            raise BackendPolicyError(
                f"Backend {backend_id} does not support model {config.llm.model!r}."
            )
        decoder_metadata = preflight_image_decoders()
        image_preflight = getattr(backend, "preflight_image", backend.preflight)
        backend_metadata = {**image_preflight(), **decoder_metadata}
    else:
        backend_metadata = {}
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

    action_priority = {"none": 0, "adopt": 1, "restore": 2, "fetch": 3, "gate": 4}
    group_actions = {
        key: max(
            (actions[str(row["resource_image_id"])] for row in group),
            key=action_priority.__getitem__,
        )
        for key, group in selected_groups.items()
    }
    fetch_urls: set[str] = set()
    ready_groups: list[tuple[tuple[str, str], list[Any]]] = []
    for key, group in selected_groups.items():
        group_action = group_actions[key]
        if group_action == "fetch":
            fetch_urls.add(key[0])
            ready_groups.append((key, group))
            continue
        changed_rows = 0
        for row in all_groups[key]:
            image_id = str(row["resource_image_id"])
            row_action = _row_action(
                row, target=targets[image_id], legacy_target=legacy_targets[image_id],
                gate=gates[image_id], settings=settings, force=force,
            )
            if row_action == "gate":
                values, retained = _gate_values(
                    row, reason=gates[image_id], target=targets[image_id], backend_id=backend_id,
                    model=config.llm.model, settings=settings,
                )
                store.apply_image_analysis([image_id], values)
                report.gate_ignored_rows += 1
                report.retained_current_rows += int(retained)
                report.ignored_reasons[gates[image_id]] += int(
                    str(row["resource_id"]) not in selected_resources
                )
            elif row_action == "restore":
                store.apply_image_analysis([image_id], _restore_gate_values(row, target=targets[image_id]))
            elif row_action == "adopt":
                store.apply_image_analysis([image_id], _adopt_target_values(row, target=targets[image_id]))
            else:
                continue
            changed_rows += 1
            touched_resources.add(str(row["resource_id"]))
        report.propagated_rows += changed_rows
        finish_group(key, group)

    effective_parallelism = report.llm_parallelism
    temporary_parent = Path(vault_root) / ".feedian" / "tmp"
    temporary_parent.mkdir(parents=True, exist_ok=True)
    groups_by_url: dict[str, list[tuple[tuple[str, str], list[Any]]]] = {}
    for key, group in ready_groups:
        groups_by_url.setdefault(key[0], []).append((key, group))

    open_run_ids: set[str] = set()
    logical_requests: dict[str, dict[str, Any]] = {}
    input_token_samples: list[int] = []

    def logical_request(key: tuple[str, str], fetched: ImageFetchResult) -> dict[str, Any]:
        return {
            "source_url": key[0], "alt_text": key[1],
            "image_sha256": fetched.image_sha256,
            "media_type": fetched.media_type,
            "download_bytes": fetched.download_bytes,
            "header_width": fetched.width, "header_height": fetched.height,
            "oriented_width": fetched.oriented_width, "oriented_height": fetched.oriented_height,
            "sent_width": fetched.sent_width, "sent_height": fetched.sent_height,
            "sent_media_type": fetched.sent_media_type or fetched.media_type,
            "resized": fetched.resized,
            "max_long_edge_pixels": settings.max_long_edge_pixels,
            "short_edge_floor_applied": fetched.short_edge_floor_applied,
            "prompt_version": IMAGE_OCR_PROMPT_VERSION,
            "schema_version": IMAGE_OCR_SCHEMA_VERSION,
        }

    def start_run(key: tuple[str, str], group: list[Any], fetched: ImageFetchResult) -> str:
        fingerprint = analysis_fingerprint(fetched.image_sha256, key[1], backend_id)
        representative = group[0]
        run_id = store.start_llm_run(
            resource_id=str(representative["resource_id"]),
            resource_revision_id=str(representative["resource_revision_id"]),
            operation="image-ocr", backend=backend_id, model=config.llm.model,
            prompt_version=IMAGE_OCR_PROMPT_VERSION,
            summary_schema_version=IMAGE_OCR_SCHEMA_VERSION,
            input_fingerprint=fingerprint,
            request={"logical": logical_request(key, fetched), "actual": None},
            auth_mode=backend.capabilities.auth_mode,
            billing_mode=backend.capabilities.billing_mode,
            backend_metadata=backend_metadata,
        )
        open_run_ids.add(run_id)
        logical_requests[run_id] = logical_request(key, fetched)
        report.llm_requests += 1
        return run_id

    def complete_group(
        key: tuple[str, str], selected_group: list[Any], fetched: ImageFetchResult,
        result: ImageAnalysisResult, run_id: str | None, duration_ms: int,
    ) -> None:
        fingerprint = analysis_fingerprint(
            fetched.image_sha256, key[1], backend_id, svg=fetched.media_type == "image/svg+xml",
        ) if fetched.image_sha256 else ""
        if run_id is not None and result.audit is not None:
            backend_request = result.audit.request
            backend_logical = backend_request.get("logical") if isinstance(backend_request, dict) else None
            actual_request = (
                backend_request.get("actual")
                if isinstance(backend_request, dict) and "actual" in backend_request
                else backend_request
            )
            logical = dict(logical_requests[run_id])
            if isinstance(backend_logical, dict):
                logical.update(backend_logical)
            store.finish_llm_run(
                run_id, request={"logical": logical, "actual": actual_request},
                response=result.audit.response,
                result=result.audit.result, usage=result.audit.usage,
                auth_mode=result.audit.auth_mode, billing_mode=result.audit.billing_mode,
                backend_metadata=result.audit.metadata, duration_ms=duration_ms,
            )
            open_run_ids.discard(run_id)
            report.input_tokens += int(result.audit.usage.get("input_tokens", 0))
            input_token_samples.append(int(result.audit.usage.get("input_tokens", 0)))
            report.output_tokens += int(result.audit.usage.get("output_tokens", 0))
            actual_cost = result.audit.response.get("total_cost_usd")
            if not isinstance(actual_cost, (int, float)) or isinstance(actual_cost, bool):
                actual_cost = result.audit.metadata.get("cli_estimated_cost_usd")
            if (
                isinstance(actual_cost, (int, float))
                and not isinstance(actual_cost, bool)
                and actual_cost >= 0
            ):
                report.cost_usd += float(actual_cost)
                report.priced_requests += 1
            elif result.audit.billing_mode == "metered-api":
                report.unpriced += 1
            else:
                report.unmetered += 1
        elif run_id is not None:
            store.finish_llm_run(
                run_id, request={"logical": logical_requests[run_id], "actual": None},
                error=result.warning or result.failure_kind or "image analysis failed",
                duration_ms=duration_ms,
            )
            open_run_ids.discard(run_id)
            if backend.capabilities.billing_mode == "metered-api":
                report.unpriced += 1
            else:
                report.unmetered += 1
        ids = [str(row["resource_image_id"]) for row in all_groups[key]]
        target = targets[str(selected_group[0]["resource_image_id"])]
        values = _attempt_values(
            result, fetched, target, fingerprint, backend_id, config.llm.model, settings, run_id,
        )
        terminal = fetched.status == "failed" and not fetched.transient
        if terminal:
            retained = 0
            for row in all_groups[key]:
                row_values, kept = _terminal_values(
                    row, fetched=fetched, target=target, backend_id=backend_id,
                    model=config.llm.model, settings=settings, fingerprint=fingerprint,
                )
                store.apply_image_analysis([str(row["resource_image_id"])], row_values)
                retained += int(kept)
            report.terminal_unavailable_rows += len(ids)
            report.retained_current_rows += retained
            report.failure_kinds[_terminal_reason(fetched.reason)] += len(ids)
        elif result.status == "failed":
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
                if (
                    fetched.image_sha256
                    and row["image_sha256"]
                    and fetched.image_sha256 != row["image_sha256"]
                ):
                    row_values["analysis_status"] = "pending"
                elif str(row["analysis_status"] or "pending") not in {"completed", "ignored"}:
                    row_values["analysis_status"] = "failed"
                store.apply_image_analysis([str(row["resource_image_id"])], row_values)
        else:
            store.apply_image_analysis(ids, values)
        count = len(ids)
        report.propagated_rows += count
        touched_resources.update(str(row["resource_id"]) for row in all_groups[key])
        if terminal:
            pass
        elif result.status == "completed":
            report.ocr_truncated += count if result.ocr_truncated else 0
            if fetched.media_type == "image/svg+xml":
                report.svg_completed_groups += 1
            else:
                report.llm_explanatory_groups += 1
        elif result.status == "ignored":
            if fetched.status == "ignored" or fetched.media_type == "image/svg+xml":
                report.postfetch_ignored_rows += count
            if fetched.media_type == "image/svg+xml":
                report.svg_ignored_groups += 1
            elif fetched.status == "ready":
                report.llm_ignored_groups += 1
            report.ignored_reasons[result.ignored_reason or "unknown"] += count
        else:
            report.transient_failed_rows += count
            report.failure_kinds[result.failure_kind or "unknown"] += count
        finish_group(key, selected_group)

    fetch_queue = deque(sorted(fetch_urls))
    pending_analysis: deque[tuple[tuple[str, str], list[Any], ImageFetchResult]] = deque()
    fetch_futures: dict[Future[Any], str] = {}
    analysis_futures: dict[
        Future[Any], tuple[tuple[str, str], list[Any], ImageFetchResult, str | None, bool]
    ] = {}
    live_ready_urls: set[str] = set()
    remaining_groups_by_url: dict[str, int] = {}
    temporary_paths: set[Path] = set()
    active_raster = 0

    def pop_schedulable_analysis(
    ) -> tuple[tuple[str, str], list[Any], ImageFetchResult] | None:
        for index, item in enumerate(pending_analysis):
            is_raster = item[2].media_type != "image/svg+xml"
            if not is_raster or active_raster < effective_parallelism:
                pending_analysis.rotate(-index)
                selected_item = pending_analysis.popleft()
                pending_analysis.rotate(index)
                return selected_item
        return None

    try:
        with ThreadPoolExecutor(max_workers=settings.workers) as executor:
            while fetch_queue or fetch_futures or pending_analysis or analysis_futures:
                while len(fetch_futures) + len(analysis_futures) < settings.workers:
                    item = pop_schedulable_analysis()
                    if item is not None:
                        key, group, fetched = item
                        is_raster = fetched.media_type != "image/svg+xml"
                        run_id = start_run(key, group, fetched) if is_raster else None
                        future = executor.submit(
                            _timed_analysis_from_fetch, fetched, backend=backend,
                            model=config.llm.model, alt_text=key[1], settings=settings,
                            temporary_parent=temporary_parent,
                        )
                        analysis_futures[future] = (key, group, fetched, run_id, is_raster)
                        if is_raster:
                            active_raster += 1
                        continue
                    if (
                        fetch_queue
                        and len(live_ready_urls) + len(fetch_futures) < settings.workers
                    ):
                        url = fetch_queue.popleft()
                        future = executor.submit(fetch_image, url, config, temporary_parent)
                        fetch_futures[future] = url
                        continue
                    break
                running = set(fetch_futures) | set(analysis_futures)
                if not running:
                    raise RuntimeError("Image scheduler stalled with pending work.")
                done, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in done:
                    if future in fetch_futures:
                        url = fetch_futures.pop(future)
                        fetched = future.result()
                        report.fetched_urls += 1
                        groups = groups_by_url[url]
                        if fetched.status == "ready":
                            live_ready_urls.add(url)
                            remaining_groups_by_url[url] = len(groups)
                            if fetched.temporary_path is not None:
                                temporary_paths.add(fetched.temporary_path)
                            for key, group in groups:
                                pending_analysis.append((key, group, fetched))
                        else:
                            result = _analysis_from_fetch(
                                fetched, backend=backend, model=config.llm.model,
                                alt_text="", settings=settings,
                                temporary_parent=temporary_parent,
                            )
                            for key, group in groups:
                                complete_group(key, group, fetched, result, None, 0)
                        continue
                    key, group, fetched, run_id, is_raster = analysis_futures.pop(future)
                    if is_raster:
                        active_raster -= 1
                    result, duration_ms = future.result()
                    complete_group(key, group, fetched, result, run_id, duration_ms)
                    remaining_groups_by_url[key[0]] -= 1
                    if remaining_groups_by_url[key[0]] == 0:
                        live_ready_urls.discard(key[0])
                        del remaining_groups_by_url[key[0]]
                        if fetched.temporary_path is not None:
                            fetched.temporary_path.unlink(missing_ok=True)
                            temporary_paths.discard(fetched.temporary_path)
    finally:
        for future in fetch_futures:
            future.cancel()
        for future in analysis_futures:
            future.cancel()
        for run_id in open_run_ids:
            store.finish_llm_run(run_id, error="image enrichment interrupted")
        for path in temporary_paths:
            path.unlink(missing_ok=True)
    report.propagated_resources = len(touched_resources)
    refreshed_rows = store.resource_images_for_enrichment()
    remaining_resources: set[str] = set()
    for row in refreshed_rows:
        target = attempt_target(
            str(row["source_url"]), str(row["alt_text"] or ""), backend_id, settings,
        )
        legacy_target = _legacy_attempt_target(
            str(row["source_url"]), str(row["alt_text"] or ""), backend_id, settings,
        )
        gate = gate_decision(str(row["source_url"]), settings)
        if _row_action(
            row, target=target, legacy_target=legacy_target, gate=gate,
            settings=settings, force=False,
        ) != "none":
            remaining_resources.add(str(row["resource_id"]))
    report.remaining_resources_after = len(remaining_resources)
    if input_token_samples:
        ordered = sorted(input_token_samples)
        report.input_tokens_per_request_avg = sum(ordered) / len(ordered)
        report.input_tokens_per_request_p50 = ordered[max(0, math.ceil(len(ordered) * 0.50) - 1)]
        report.input_tokens_per_request_p95 = ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]
        report.input_tokens_per_request_max = ordered[-1]
    return report


def report_line(report: ImageEnrichmentReport) -> str:
    cost = f"{report.cost_usd:.6f}" if report.priced_requests else "unknown"
    token_avg = (
        f"{report.input_tokens_per_request_avg:.1f}"
        if report.input_tokens_per_request_avg is not None else "unknown"
    )
    return (
        f"remaining_resources_before={report.remaining_resources_before} "
        f"remaining_resources_after={report.remaining_resources_after} "
        f"selected_resources={report.selected_resources} fetched_urls={report.fetched_urls} "
        f"llm_requests={report.llm_requests} llm_explanatory_groups={report.llm_explanatory_groups} "
        f"llm_ignored_groups={report.llm_ignored_groups} svg_completed_groups={report.svg_completed_groups} "
        f"svg_ignored_groups={report.svg_ignored_groups} gate_ignored_rows={report.gate_ignored_rows} "
        f"postfetch_ignored_rows={report.postfetch_ignored_rows} "
        f"terminal_unavailable_rows={report.terminal_unavailable_rows} "
        f"retained_current_rows={report.retained_current_rows} "
        f"transient_failed_rows={report.transient_failed_rows} "
        f"propagated_rows={report.propagated_rows} propagated_resources={report.propagated_resources} "
        f"ocr_truncated={report.ocr_truncated} input_tokens={report.input_tokens} "
        f"output_tokens={report.output_tokens} input_tokens_per_request_avg={token_avg} "
        f"input_tokens_per_request_p50={report.input_tokens_per_request_p50 if report.input_tokens_per_request_p50 is not None else 'unknown'} "
        f"input_tokens_per_request_p95={report.input_tokens_per_request_p95 if report.input_tokens_per_request_p95 is not None else 'unknown'} "
        f"input_tokens_per_request_max={report.input_tokens_per_request_max if report.input_tokens_per_request_max is not None else 'unknown'} "
        f"cost_usd={cost} unpriced={report.unpriced} "
        f"unmetered={report.unmetered} llm_parallelism={report.llm_parallelism}"
    )
