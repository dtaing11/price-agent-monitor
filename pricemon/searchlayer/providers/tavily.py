"""Tavily - a search API built for agents, with a documented free tier."""

from __future__ import annotations

from ..models import SearchResult
from .base import HttpProvider


class TavilyProvider(HttpProvider):
    name = "tavily"
    endpoint = "https://api.tavily.com/search"

    def build_request(self, query: str, count: int, lang: str, api_key: str) -> dict:
        return {
            "method": "POST",
            "url": self.config.endpoint or self.endpoint,
            "json": {
                "query": query,
                "max_results": max(1, min(count, 20)),
                "search_depth": "basic",
            },
            "headers": {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
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
