"""A SearXNG instance you run yourself - your machine, your quota."""

from __future__ import annotations

from ..errors import MalformedResponse
from ..models import SearchResult
from .base import HttpProvider


class SearxngProvider(HttpProvider):
    name = "searxng"

    @property
    def configured(self) -> bool:
        return bool(self.config.endpoint)

    def build_request(self, query: str, count: int, lang: str, api_key: str) -> dict:
        if not self.config.endpoint:
            raise MalformedResponse(self.name, "no endpoint configured")
        return {
            "method": "GET",
            "url": self.config.endpoint.rstrip("/") + "/search",
            "params": {"q": query, "format": "json", "language": lang},
            "headers": {"Accept": "application/json"},
        }

    def parse(self, payload: dict) -> list[SearchResult]:
        out: list[SearchResult] = []
        for position, hit in enumerate((payload or {}).get("results") or [], start=1):
            url = hit.get("url")
            if not url:
                continue
            out.append(
                SearchResult(
                    url=url,
                    title=hit.get("title") or "",
                    snippet=hit.get("content") or "",
                    rank=position,
                    provider=self.name,
                )
            )
        return out
