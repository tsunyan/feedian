from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar
from urllib.error import HTTPError, URLError


T = TypeVar("T")
TRANSIENT_HTTP_STATUS_CODES = {408, 409, 425, 429}


def run_with_retries(
    operation: Callable[[], T],
    max_retries: int,
    retry_base_seconds: float,
) -> T:
    for attempt in range(max_retries + 1):
        try:
            return operation()
        except (HTTPError, URLError, TimeoutError, ConnectionError) as exc:
            if attempt >= max_retries or not is_transient_error(exc):
                raise
            delay = retry_delay_seconds(exc, retry_base_seconds, attempt)
            if isinstance(exc, HTTPError):
                exc.close()
            time.sleep(delay)
    raise AssertionError("retry loop exited without returning or raising")


def is_transient_error(exc: Exception) -> bool:
    if isinstance(exc, HTTPError):
        return exc.code in TRANSIENT_HTTP_STATUS_CODES or 500 <= exc.code <= 599
    return True


def retry_delay_seconds(exc: Exception, retry_base_seconds: float, attempt: int) -> float:
    if isinstance(exc, HTTPError):
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        if retry_after:
            try:
                return min(60.0, max(0.0, float(retry_after)))
            except ValueError:
                pass
    return retry_base_seconds * (2**attempt)
