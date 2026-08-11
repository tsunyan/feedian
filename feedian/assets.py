from __future__ import annotations

from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, Request, build_opener
import ssl

from .extract import SafeRedirectHandler, validate_fetch_url


@dataclass(frozen=True)
class BinaryFetchResult:
    url: str
    final_url: str = ""
    body: bytes | None = None
    media_type: str = ""
    headers: dict[str, str] | None = None
    status: int | None = None
    error: str | None = None
    too_large: bool = False


def fetch_image(
    url: str,
    *,
    timeout_seconds: int = 30,
    max_bytes: int = 100 * 1024 * 1024,
    allow_private_urls: bool = False,
) -> BinaryFetchResult:
    """Fetch one image while applying the same SSRF policy as HTML collection."""
    try:
        validate_fetch_url(url, allow_private_urls=allow_private_urls)
        request = Request(
            url,
            headers={"User-Agent": "feedian/0.1 (+https://github.com/) Python urllib", "Accept": "image/*,*/*;q=0.1"},
            method="GET",
        )
        context = ssl.create_default_context()
        opener = build_opener(HTTPSHandler(context=context), SafeRedirectHandler(allow_private_urls))
        with opener.open(request, timeout=timeout_seconds) as response:
            media_type = str(response.headers.get("Content-Type", "")).split(";", 1)[0].lower()
            if not media_type.startswith("image/"):
                return BinaryFetchResult(url=url, error=f"unsupported image type: {media_type or 'unknown'}")
            body = response.read(max_bytes + 1)
            final_url = response.geturl()
            headers = {str(key): str(value) for key, value in response.headers.items()}
            status = getattr(response, "status", None)
    except HTTPError as exc:
        return BinaryFetchResult(url=url, error=f"HTTP {exc.code}", status=exc.code)
    except URLError as exc:
        return BinaryFetchResult(url=url, error=str(exc.reason))
    except Exception as exc:
        return BinaryFetchResult(url=url, error=str(exc))
    if len(body) > max_bytes:
        return BinaryFetchResult(
            url=url, final_url=final_url, media_type=media_type, headers=headers,
            status=status if isinstance(status, int) else None, error=f"image exceeds {max_bytes} bytes", too_large=True,
        )
    return BinaryFetchResult(
        url=url, final_url=final_url, body=body, media_type=media_type, headers=headers,
        status=status if isinstance(status, int) else None,
    )
