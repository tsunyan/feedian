from __future__ import annotations

import json
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
    ) -> None:
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_base_seconds = retry_base_seconds

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
        with urlopen(request, timeout=self.timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
