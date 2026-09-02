"""Shared machinery for HTTP-backed providers.

Every adapter's job is the same: send one request, translate the answer into
SearchResults, and translate every failure into one of the four errors the
router understands. Nothing provider-specific leaks past here.
"""

from __future__ import annotations

import httpx

from ..errors import MalformedResponse, ProviderTimeout, RateLimited, Upstream5xx
from ..models import ProviderConfig, ProviderHealth, SearchResult


class HttpProvider:
    """Base for providers that are a single authenticated GET or POST."""

    name = "http"
    endpoint = ""

    def __init__(self, config: ProviderConfig, client: httpx.AsyncClient | None = None):
        self.config = config
        self._client = client
        self.last_headers: httpx.Headers | None = None

    # -- to implement -----------------------------------------------------
    def build_request(self, query: str, count: int, lang: str, api_key: str) -> dict:
        raise NotImplementedError

    def parse(self, payload: dict) -> list[SearchResult]:
        raise NotImplementedError

    # -- shared -----------------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self.config.api_keys) or bool(self.config.endpoint)

    def _key(self) -> str:
        return self.config.api_keys[0] if self.config.api_keys else ""

    async def _send(self, request: dict) -> dict:
        client = self._client or httpx.AsyncClient(timeout=self.config.timeout)
        owned = self._client is None
        try:
            response = await client.request(
                request.get("method", "GET"),
                request["url"],
                params=request.get("params"),
                json=request.get("json"),
                headers=request.get("headers"),
                timeout=self.config.timeout,
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(self.name, str(exc)) from exc
        except httpx.HTTPError as exc:
            raise Upstream5xx(self.name, str(exc)) from exc
        finally:
            if owned:
                await client.aclose()

        self.last_headers = response.headers

        if response.status_code == 429:
            raise RateLimited(
                self.name, _retry_after(response.headers), "quota exhausted upstream"
            )
        if response.status_code in (402, 403) and "quota" in response.text.lower():
            # Some providers bill a spent quota as a permissions problem.
            raise RateLimited(
                self.name, _retry_after(response.headers), response.text[:120]
            )
        if response.status_code >= 500:
            raise Upstream5xx(self.name, f"HTTP {response.status_code}")
        if response.status_code >= 400:
            raise MalformedResponse(
                self.name, f"HTTP {response.status_code}: {response.text[:160]}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise MalformedResponse(self.name, "response was not JSON") from exc

    async def search(
        self, query: str, count: int = 10, lang: str = "en"
    ) -> list[SearchResult]:
        payload = await self._send(self.build_request(query, count, lang, self._key()))
        try:
            return self.parse(payload)
        except MalformedResponse:
            raise
        except Exception as exc:
            raise MalformedResponse(self.name, f"unexpected shape: {exc}") from exc

    async def health(self) -> ProviderHealth:
        return ProviderHealth(name=self.name, configured=self.configured)


def _retry_after(headers) -> float | None:
    for name in ("retry-after", "x-ratelimit-reset", "ratelimit-reset"):
        raw = headers.get(name)
        if raw:
            try:
                return float(raw)
            except (TypeError, ValueError):
                continue
    return None
