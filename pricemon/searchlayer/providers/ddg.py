"""DuckDuckGo, demoted.

There is no public DuckDuckGo API: any client for it scrapes an HTML endpoint,
which means no key, no quota to reserve, and a throttle that cannot be retried
away from a datacentre IP. So it is last resort only - never chosen first,
never the only provider tried - and every way it can say "slow down",
including a 202 with an empty body, is translated into RateLimited so the
router treats it like any other spent budget.
"""

from __future__ import annotations

import asyncio

import httpx

from ..errors import MalformedResponse, ProviderTimeout, RateLimited, Upstream5xx
from ..models import ProviderConfig, ProviderHealth, SearchResult

HTML_ENDPOINT = "https://html.duckduckgo.com/html/"


class DDGProvider:
    name = "ddg"

    def __init__(self, config: ProviderConfig, client: httpx.AsyncClient | None = None):
        self.config = config
        self._client = client
        self.last_headers: httpx.Headers | None = None

    @property
    def configured(self) -> bool:
        return True  # needs no credentials, which is the whole problem

    async def search(
        self, query: str, count: int = 10, lang: str = "en"
    ) -> list[SearchResult]:
        results = await asyncio.to_thread(self._search_via_library, query, count)
        if results is not None:
            return results
        return await self._search_via_html(query, count)

    def _search_via_library(self, query: str, count: int):
        """Use the ddgs library when installed, mapping its exceptions."""
        try:
            from ddgs import DDGS  # type: ignore[import-not-found]
        except ImportError:
            try:
                from duckduckgo_search import DDGS  # type: ignore[import-not-found]
            except ImportError:
                return None
        try:
            with DDGS() as ddgs:
                hits = list(ddgs.text(query, max_results=count))
        except Exception as exc:
            name = type(exc).__name__.lower()
            if "ratelimit" in name or "429" in str(exc) or "202" in str(exc):
                raise RateLimited(self.name, None, str(exc)[:120]) from exc
            if "timeout" in name:
                raise ProviderTimeout(self.name, str(exc)[:120]) from exc
            raise Upstream5xx(self.name, str(exc)[:120]) from exc

        return [
            SearchResult(
                url=hit.get("href") or hit.get("url") or "",
                title=hit.get("title") or "",
                snippet=hit.get("body") or "",
                rank=position,
                provider=self.name,
            )
            for position, hit in enumerate(hits, start=1)
            if (hit.get("href") or hit.get("url") or "").startswith("http")
        ]

    async def _search_via_html(self, query: str, count: int) -> list[SearchResult]:
        from urllib.parse import parse_qs, unquote, urlparse

        from bs4 import BeautifulSoup

        client = self._client or httpx.AsyncClient(timeout=self.config.timeout)
        owned = self._client is None
        try:
            response = await client.get(
                HTML_ENDPOINT,
                params={"q": query},
                headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"},
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(self.name, str(exc)) from exc
        except httpx.HTTPError as exc:
            raise Upstream5xx(self.name, str(exc)) from exc
        finally:
            if owned:
                await client.aclose()

        self.last_headers = response.headers
        body = response.text

        # DuckDuckGo answers a throttle with 202 and an "anomaly" page rather
        # than a 429, so status alone would read as success.
        if response.status_code == 202 or "anomaly" in body[:4000].lower():
            raise RateLimited(self.name, None, "throttled (202/anomaly page)")
        if response.status_code == 429:
            raise RateLimited(self.name, None, "throttled")
        if response.status_code >= 500:
            raise Upstream5xx(self.name, f"HTTP {response.status_code}")
        if response.status_code >= 400:
            raise MalformedResponse(self.name, f"HTTP {response.status_code}")

        soup = BeautifulSoup(body, "html.parser")
        anchors = soup.select("a.result__a")
        if not anchors and "result__a" not in body:
            raise RateLimited(
                self.name, None, "no results markup - treated as a throttle"
            )

        out: list[SearchResult] = []
        for position, anchor in enumerate(anchors[:count], start=1):
            raw_href = anchor.get("href") or ""
            href = " ".join(raw_href) if isinstance(raw_href, list) else str(raw_href)
            if "uddg=" in href:
                href = unquote(parse_qs(urlparse(href).query).get("uddg", [""])[0])
            if href.startswith("http"):
                out.append(
                    SearchResult(
                        url=href,
                        title=anchor.get_text(" ", strip=True),
                        rank=position,
                        provider=self.name,
                    )
                )
        return out

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            name=self.name,
            configured=True,
            note="best-effort last resort; scraped endpoint with no reservable quota",
        )
