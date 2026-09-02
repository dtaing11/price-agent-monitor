"""Fixtures for the search layer: fake providers that behave like real ones."""

from __future__ import annotations

import asyncio

import pytest

from pricemon.searchlayer.errors import RateLimited
from pricemon.searchlayer.models import ProviderConfig, ProviderHealth, SearchResult


class FakeProvider:
    """A provider you can make succeed, throttle, or stall on demand."""

    def __init__(
        self,
        name: str = "fake",
        results: int = 3,
        always_429: bool = False,
        delay: float = 0.0,
        config: ProviderConfig | None = None,
    ):
        self.name = name
        self.config = config or ProviderConfig(name=name, per_minute=1000)
        self.results = results
        self.always_429 = always_429
        self.delay = delay
        self.calls = 0
        self.configured = True
        self.last_headers = None

    async def search(self, query: str, count: int = 10, lang: str = "en"):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.always_429:
            raise RateLimited(self.name, 30.0, "always throttled")
        return [
            SearchResult(
                url=f"https://{self.name}.test/{abs(hash(query)) % 1000}/{i}",
                title=f"{query} result {i}",
                rank=i,
                provider=self.name,
            )
            for i in range(1, self.results + 1)
        ]

    async def health(self) -> ProviderHealth:
        return ProviderHealth(name=self.name, configured=True)


@pytest.fixture
def fake_provider():
    return FakeProvider
