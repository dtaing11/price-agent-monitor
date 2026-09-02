"""Exa - an official API, useful when a query is described rather than typed."""

from __future__ import annotations

from ..models import SearchResult
from .base import HttpProvider


class ExaProvider(HttpProvider):
    name = "exa"
    endpoint = "https://api.exa.ai/search"

    def build_request(self, query: str, count: int, lang: str, api_key: str) -> dict:
        return {
            "method": "POST",
            "url": self.config.endpoint or self.endpoint,
            "json": {
                "query": query,
                "numResults": max(1, min(count, 25)),
                "type": "auto",
            },
            "headers": {"Content-Type": "application/json", "x-api-key": api_key},
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
                    snippet=(hit.get("text") or hit.get("summary") or "")[:400],
                    rank=position,
                    provider=self.name,
                )
            )
        return out
