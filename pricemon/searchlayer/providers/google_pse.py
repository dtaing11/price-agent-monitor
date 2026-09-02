"""Google Programmable Search - official, keyed, 100 free queries a day."""

from __future__ import annotations

from ..models import SearchResult
from .base import HttpProvider


class GooglePSEProvider(HttpProvider):
    name = "google_pse"
    endpoint = "https://www.googleapis.com/customsearch/v1"

    def __init__(self, config, client=None, cx: str | None = None):
        super().__init__(config, client)
        # The engine id is not a secret, so it rides in the config endpoint
        # slot as "cx:<id>" when not passed explicitly.
        self.cx = cx or (config.endpoint or "").replace("cx:", "") or ""

    @property
    def configured(self) -> bool:
        return bool(self.config.api_keys and self.cx)

    def build_request(self, query: str, count: int, lang: str, api_key: str) -> dict:
        return {
            "method": "GET",
            "url": self.endpoint,
            "params": {
                "key": api_key,
                "cx": self.cx,
                "q": query,
                "num": max(1, min(count, 10)),  # the API's own ceiling
                "hl": lang,
            },
        }

    def parse(self, payload: dict) -> list[SearchResult]:
        out: list[SearchResult] = []
        for position, hit in enumerate((payload or {}).get("items") or [], start=1):
            url = hit.get("link")
            if not url:
                continue
            out.append(
                SearchResult(
                    url=url,
                    title=hit.get("title") or "",
                    snippet=hit.get("snippet") or "",
                    rank=position,
                    provider=self.name,
                )
            )
        return out
