from __future__ import annotations

import json
import time
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .retry import run_with_retries


API_BASE = "https://api.raindrop.io/rest/v1"


class RaindropClient:
    def __init__(
        self,
        token: str,
        timeout_seconds: int = 30,
        max_retries: int = 3,
        retry_base_seconds: float = 1.0,
        request_interval_seconds: float = 0.0,
    ) -> None:
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_base_seconds = retry_base_seconds
        self.request_interval_seconds = max(0.0, request_interval_seconds)
        self._next_request_at = 0.0

    def iter_raindrops(
        self,
        collection_id: int,
        per_page: int,
        nested: bool,
        limit: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        page = 0
        yielded = 0
        while True:
            params = {
                "page": page,
                "perpage": min(max(1, per_page), 50),
                "sort": "-created",
                "nested": str(bool(nested)).lower(),
            }
            data = self._get(f"/raindrops/{collection_id}", params)
            items = data.get("items") or []
            if not items:
                break
            for item in items:
                yield item
                yielded += 1
                if limit is not None and yielded >= limit:
                    return
            if len(items) < params["perpage"]:
                break
            page += 1

    def get_root_collections(self) -> list[dict[str, Any]]:
        data = self._get("/collections", {})
        return list(data.get("items") or [])

    def get_child_collections(self) -> list[dict[str, Any]]:
        data = self._get("/collections/childrens", {})
        return list(data.get("items") or [])

    def get_raindrop(self, raindrop_id: int) -> dict[str, Any]:
        data = self._get(f"/raindrop/{raindrop_id}", {})
        item = data.get("item")
        return item if isinstance(item, dict) else {}

    def update_raindrop_note(self, raindrop_id: int, note: str) -> None:
        request = Request(
            f"{API_BASE}/raindrop/{raindrop_id}",
            data=json.dumps({"note": note}).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "raindian/0.1",
            },
            method="PUT",
        )
        try:
            run_with_retries(
                lambda: self._read_json(request),
                max_retries=self.max_retries,
                retry_base_seconds=self.retry_base_seconds,
            )
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Raindrop API error HTTP {exc.code}: {body}") from exc
        except URLError as exc:
            raise RuntimeError(f"Raindrop API network error: {exc.reason}") from exc

    def append_raindrop_tags(self, collection_id: int, raindrop_id: int, tags: list[str]) -> None:
        request = Request(
            f"{API_BASE}/raindrops/{collection_id}",
            data=json.dumps({"ids": [raindrop_id], "tags": tags}).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "raindian/0.1",
            },
            method="PUT",
        )
        try:
            run_with_retries(
                lambda: self._read_json(request),
                max_retries=self.max_retries,
                retry_base_seconds=self.retry_base_seconds,
            )
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Raindrop API error HTTP {exc.code}: {body}") from exc
        except URLError as exc:
            raise RuntimeError(f"Raindrop API network error: {exc.reason}") from exc

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        query = f"?{urlencode(params)}" if params else ""
        request = Request(
            f"{API_BASE}{path}{query}",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "User-Agent": "raindian/0.1",
            },
            method="GET",
        )
        try:
            return run_with_retries(
                lambda: self._read_json(request),
                max_retries=self.max_retries,
                retry_base_seconds=self.retry_base_seconds,
            )
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Raindrop API error HTTP {exc.code}: {body}") from exc
        except URLError as exc:
            raise RuntimeError(f"Raindrop API network error: {exc.reason}") from exc

    def _read_json(self, request: Request) -> dict[str, Any]:
        self._wait_for_request_slot()
        with urlopen(request, timeout=self.timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))

    def _wait_for_request_slot(self) -> None:
        if self.request_interval_seconds <= 0:
            return
        now = time.monotonic()
        delay = self._next_request_at - now
        if delay > 0:
            time.sleep(delay)
        self._next_request_at = max(now, self._next_request_at) + self.request_interval_seconds
