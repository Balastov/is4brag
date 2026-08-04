"""Thin standard-library client for the search API."""

from __future__ import annotations

import json
from typing import Callable, Mapping, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class SearchClientError(RuntimeError):
    pass


class SearchClient:
    def __init__(self, url: str, *, timeout: float = 15.0, opener=None) -> None:
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.opener = opener or urlopen

    def search(
        self,
        query: str,
        *,
        top_k: int = 10,
        sections: Optional[Sequence[str]] = None,
        use_parents: bool = True,
        filters: Optional[Mapping[str, str]] = None,
    ) -> list[dict]:
        payload = json.dumps(
            {
                "query": query,
                "top_k": top_k,
                "sections": list(sections) if sections else None,
                "use_parents": use_parents,
                "filters": dict(filters or {}),
            }
        ).encode("utf-8")
        request = Request(
            self.url + "/search",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                decoded = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, OSError, ValueError) as exc:
            raise SearchClientError("search API unavailable") from exc
        if not isinstance(decoded, dict) or not isinstance(decoded.get("results"), list):
            raise SearchClientError("search API returned an invalid response")
        return decoded["results"]


def search_with_fallback(
    client: SearchClient,
    fallback: Callable[[], list[dict]],
    **search_arguments,
) -> list[dict]:
    try:
        return client.search(**search_arguments)
    except SearchClientError:
        return fallback()
