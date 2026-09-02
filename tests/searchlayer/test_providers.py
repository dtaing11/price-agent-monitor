"""Adapters, against mocked HTTP. No key needed, no network touched.

The contract each one must keep: parse a real response shape, and translate
every failure into exactly one of the four errors the router understands.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from pricemon.searchlayer.errors import (
    MalformedResponse,
    ProviderTimeout,
    RateLimited,
    Upstream5xx,
)
from pricemon.searchlayer.models import ProviderConfig
from pricemon.searchlayer.providers.brave import BraveProvider
from pricemon.searchlayer.providers.ddg import DDGProvider
from pricemon.searchlayer.providers.exa import ExaProvider
from pricemon.searchlayer.providers.google_pse import GooglePSEProvider
from pricemon.searchlayer.providers.searxng import SearxngProvider
from pricemon.searchlayer.providers.tavily import TavilyProvider

pytestmark = pytest.mark.asyncio

BRAVE_BODY = {
    "web": {
        "results": [
            {"url": "https://shop.test/p/1", "title": "Dock", "description": "A dock"},
            {"url": "https://shop.test/p/2", "title": "Dock 2", "description": ""},
        ]
    }
}
TAVILY_BODY = {
    "results": [
        {"url": "https://shop.test/p/9", "title": "Hub", "content": "A hub"},
    ]
}


@respx.mock
async def test_brave_parses_and_ranks():
    respx.get("https://api.search.brave.com/res/v1/web/search").mock(
        return_value=httpx.Response(200, json=BRAVE_BODY)
    )
    provider = BraveProvider(ProviderConfig(name="brave", api_keys=["k"]))
    results = await provider.search("dock", count=5)
    assert [r.url for r in results] == [
        "https://shop.test/p/1",
        "https://shop.test/p/2",
    ]
    assert results[0].rank == 1 and results[0].provider == "brave"
    assert results[0].snippet == "A dock"


@respx.mock
async def test_tavily_parses():
    respx.post("https://api.tavily.com/search").mock(
        return_value=httpx.Response(200, json=TAVILY_BODY)
    )
    provider = TavilyProvider(ProviderConfig(name="tavily", api_keys=["k"]))
    results = await provider.search("hub")
    assert results[0].url == "https://shop.test/p/9"
    assert results[0].snippet == "A hub"


@respx.mock
async def test_429_becomes_rate_limited_with_retry_after():
    respx.get("https://api.search.brave.com/res/v1/web/search").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "42"}, json={})
    )
    provider = BraveProvider(ProviderConfig(name="brave", api_keys=["k"]))
    with pytest.raises(RateLimited) as caught:
        await provider.search("dock")
    assert caught.value.retry_after == 42.0
    assert caught.value.provider == "brave"


@respx.mock
async def test_5xx_and_garbage_are_distinguished():
    respx.post("https://api.tavily.com/search").mock(return_value=httpx.Response(503))
    provider = TavilyProvider(ProviderConfig(name="tavily", api_keys=["k"]))
    with pytest.raises(Upstream5xx):
        await provider.search("hub")

    respx.post("https://api.tavily.com/search").mock(
        return_value=httpx.Response(200, content=b"<html>not json</html>")
    )
    with pytest.raises(MalformedResponse):
        await provider.search("hub")


@respx.mock
async def test_timeout_becomes_provider_timeout():
    respx.get("https://api.search.brave.com/res/v1/web/search").mock(
        side_effect=httpx.ReadTimeout("too slow")
    )
    provider = BraveProvider(ProviderConfig(name="brave", api_keys=["k"]))
    with pytest.raises(ProviderTimeout):
        await provider.search("dock")


@respx.mock
async def test_a_shape_change_is_malformed_not_a_crash():
    respx.get("https://api.search.brave.com/res/v1/web/search").mock(
        return_value=httpx.Response(200, json={"web": {"results": "not-a-list"}})
    )
    provider = BraveProvider(ProviderConfig(name="brave", api_keys=["k"]))
    with pytest.raises(MalformedResponse):
        await provider.search("dock")


@respx.mock
async def test_ddg_202_anomaly_page_is_a_rate_limit_not_success():
    """DuckDuckGo answers a throttle with 202 and an anomaly page."""
    respx.get("https://html.duckduckgo.com/html/").mock(
        return_value=httpx.Response(202, text="<html>anomaly detected</html>")
    )
    provider = DDGProvider(ProviderConfig(name="ddg"))
    with pytest.raises(RateLimited):
        await provider._search_via_html("dock", 5)


@respx.mock
async def test_ddg_empty_markup_is_also_treated_as_a_throttle():
    respx.get("https://html.duckduckgo.com/html/").mock(
        return_value=httpx.Response(200, text="<html><body>nothing here</body></html>")
    )
    provider = DDGProvider(ProviderConfig(name="ddg"))
    with pytest.raises(RateLimited):
        await provider._search_via_html("dock", 5)


@respx.mock
async def test_exa_google_and_searxng_parse_their_shapes():
    respx.post("https://api.exa.ai/search").mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"url": "https://a.test/p", "title": "A", "text": "t"}]},
        )
    )
    exa = ExaProvider(ProviderConfig(name="exa", api_keys=["k"]))
    assert (await exa.search("x"))[0].url == "https://a.test/p"

    respx.get("https://www.googleapis.com/customsearch/v1").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [{"link": "https://b.test/p", "title": "B", "snippet": "s"}]
            },
        )
    )
    google = GooglePSEProvider(
        ProviderConfig(name="google_pse", api_keys=["k"], endpoint="cx:123")
    )
    assert google.configured
    assert (await google.search("x"))[0].url == "https://b.test/p"

    respx.get("https://searx.local/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [{"url": "https://c.test/p", "title": "C", "content": "c"}]
            },
        )
    )
    searx = SearxngProvider(
        ProviderConfig(name="searxng", endpoint="https://searx.local")
    )
    assert (await searx.search("x"))[0].url == "https://c.test/p"


@respx.mock
async def test_headers_reconcile_the_ledger():
    from pricemon.searchlayer.ledger import QuotaLedger

    respx.get("https://api.search.brave.com/res/v1/web/search").mock(
        return_value=httpx.Response(
            200,
            json=BRAVE_BODY,
            headers={"X-RateLimit-Remaining": "3", "X-RateLimit-Reset": "60"},
        )
    )
    provider = BraveProvider(ProviderConfig(name="brave", api_keys=["k"]))
    await provider.search("dock")

    ledger = QuotaLedger()
    ledger.reconcile("brave", provider.last_headers)
    config = ProviderConfig(name="brave", per_minute=1000)
    assert await ledger.remaining(config) == 3, "provider's own count must win"
