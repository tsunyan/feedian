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
    *,
    transient_status_codes: frozenset[int] = frozenset(),
    max_delay_seconds: float = 60.0,
) -> T:
    """Retry a bounded number of times.

    `transient_status_codes` adds call-specific statuses to the retryable set, so
    a caller never needs its own nested retry loop; nesting one inside this
    function multiplies the attempts and makes the total wait unpredictable.
    """
    for attempt in range(max_retries + 1):
        try:
            return operation()
        except (HTTPError, URLError, TimeoutError, ConnectionError) as exc:
            if attempt >= max_retries or not is_transient_error(exc, transient_status_codes):
                raise
            delay = min(max_delay_seconds, retry_delay_seconds(exc, retry_base_seconds, attempt))
            if isinstance(exc, HTTPError):
                exc.close()
            time.sleep(delay)
    raise AssertionError("retry loop exited without returning or raising")


def is_transient_error(exc: Exception, extra_status_codes: frozenset[int] = frozenset()) -> bool:
    if isinstance(exc, HTTPError):
        return (
            exc.code in TRANSIENT_HTTP_STATUS_CODES
            or exc.code in extra_status_codes
            or 500 <= exc.code <= 599
        )
    return True


def retry_delay_seconds(exc: Exception, retry_base_seconds: float, attempt: int) -> float:
    if isinstance(exc, HTTPError):
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        retry_after_seconds: float | None = None
        if retry_after:
            try:
                retry_after_seconds = min(60.0, max(0.0, float(retry_after)))
            except ValueError:
                pass
        if exc.code == 429 and exc.headers:
            reset = exc.headers.get("X-RateLimit-Reset")
            if reset:
                try:
                    reset_seconds = max(0.0, float(reset) - time.time()) + 0.1
                    return max(retry_after_seconds or 0.0, reset_seconds)
                except ValueError:
                    pass
        if retry_after_seconds is not None:
            return retry_after_seconds
    return retry_base_seconds * (2**attempt)
