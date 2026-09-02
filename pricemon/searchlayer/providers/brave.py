"""Brave Search API - an official API with a documented free tier."""

from __future__ import annotations

from ..models import SearchResult
from .base import HttpProvider


class BraveProvider(HttpProvider):
    name = "brave"
    endpoint = "https://api.search.brave.com/res/v1/web/search"

    def build_request(self, query: str, count: int, lang: str, api_key: str) -> dict:
        return {
            "method": "GET",
            "url": self.config.endpoint or self.endpoint,
            "params": {
                "q": query,
                "count": max(1, min(count, 20)),
                "search_lang": lang,
                "safesearch": "off",
            },
            "headers": {
                "Accept": "application/json",
                "X-Subscription-Token": api_key,
            },
        }

    def parse(self, payload: dict) -> list[SearchResult]:
        hits = ((payload or {}).get("web") or {}).get("results") or []
        out: list[SearchResult] = []
        for position, hit in enumerate(hits, start=1):
            url = hit.get("url")
            if not url:
                continue
            out.append(
                SearchResult(
                    url=url,
                    title=hit.get("title") or "",
                    snippet=hit.get("description") or "",
                    rank=position,
                    provider=self.name,
                )
            )
        return out
