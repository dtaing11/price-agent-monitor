"""The acceptance criteria for the search layer, as tests.

Each test is one line of the brief: no rate-limit error reaches the caller, no
ledger window is breached, identical concurrent queries make one call, a
constantly-throttled provider is dropped within two calls, and losing Redis
degrades rather than fails.
"""

from __future__ import annotations

import asyncio

import pytest

from pricemon.searchlayer.cache import SearchCache
from pricemon.searchlayer.errors import (
    AllProvidersExhausted,
    QueryBudgetExceeded,
    RateLimited,
)
from pricemon.searchlayer.ledger import QuotaLedger, budget_for
from pricemon.searchlayer.models import ProviderConfig
from pricemon.searchlayer.router import SearchRouter

from .conftest import FakeProvider

pytestmark = pytest.mark.asyncio


def build(providers, **kwargs):
    return SearchRouter(
        providers,
        configs={p.name: p.config for p in providers},
        cache=SearchCache(),
        ledger=QuotaLedger(),
        **kwargs,
    )


async def test_thousand_queries_raise_no_rate_limit_and_breach_no_window():
    """A long run must not surface RateLimited, nor exceed a ledger window."""
    config = ProviderConfig(name="brave", per_minute=1000, per_day=5000)
    provider = FakeProvider("brave", config=config)
    router = build([provider])

    queries = [f"product model {i % 250}" for i in range(1000)]
    for query in queries:
        results = await router.search(query, count=3)  # must not raise
        assert results

    cap = budget_for(config, "minute")
    assert provider.calls <= cap, (
        f"{provider.calls} calls exceeded the {cap}/min budget"
    )
    # 1000 queries over 250 distinct questions: the rest must come from cache.
    assert router.cache.stats.hit_rate > 0.6


async def test_cache_hit_rate_over_60_percent_on_paraphrased_repeats():
    provider = FakeProvider("brave")
    router = build([provider])
    trace = [
        "Sony WH-1000XM5 price",
        "  sony   wh-1000xm5 PRICE?? ",
        "what is the price of Sony WH-1000XM5",
        "thunderbolt dock",
        "the best thunderbolt dock",
        "please find me a thunderbolt dock",
        "govee outdoor strip light",
        "govee outdoor strip lights",
    ] * 4
    for query in trace:
        await router.search(query, count=3)
    assert router.cache.stats.hit_rate > 0.6, router.cache.stats.as_dict()


async def test_fifty_concurrent_identical_queries_make_one_call():
    provider = FakeProvider("brave", delay=0.15)
    router = build([provider])
    await asyncio.gather(*(router.search("same question", count=3) for _ in range(50)))
    assert provider.calls == 1, f"{provider.calls} calls for one question"


async def test_constant_429_opens_the_breaker_within_two_calls():
    bad = FakeProvider(
        "bad", always_429=True, config=ProviderConfig(name="bad", tier=0)
    )
    good = FakeProvider("good", config=ProviderConfig(name="good", tier=1))
    router = build([bad, good])

    for i in range(6):
        results = await router.search(f"query {i}", count=3)  # never raises
        assert results
    assert bad.calls <= 2, f"kept calling a throttled provider {bad.calls} times"
    assert router.breakers["bad"].is_open
    assert good.calls >= 4, "router did not fall through to the healthy provider"


async def test_rate_limit_never_reaches_the_caller():
    only = FakeProvider("only", always_429=True)
    router = build([only], serve_stale=False)
    with pytest.raises(AllProvidersExhausted) as caught:
        await router.search("anything", count=3)
    assert caught.value.reset_at is not None, "exhaustion must carry the earliest reset"
    assert not isinstance(caught.value, RateLimited)


async def test_stale_cache_is_served_when_everything_is_exhausted():
    provider = FakeProvider("brave")
    router = build([provider])
    first = await router.search("laptop stand", count=3)
    assert first

    router.cache.memory.clear()  # fresh tier expires
    provider.always_429 = True  # and the provider is now spent
    served = await router.search("laptop stand", count=3)
    assert served, "should have fallen back to the stale tier"
    assert all("stale" in r.provider for r in served), "stale results must be flagged"


async def test_losing_redis_mid_run_keeps_serving():
    class DyingRedis:
        def __init__(self):
            self.calls = 0

        async def get(self, *a, **k):
            self.calls += 1
            if self.calls > 2:
                raise ConnectionError("redis went away")

        async def set(self, *a, **k):
            if self.calls > 2:
                raise ConnectionError("redis went away")

        def pipeline(self):
            raise ConnectionError("redis went away")

        async def zremrangebyscore(self, *a, **k):
            raise ConnectionError("redis went away")

        async def zcard(self, *a, **k):
            raise ConnectionError("redis went away")

    redis = DyingRedis()
    provider = FakeProvider("brave")
    router = SearchRouter(
        [provider],
        configs={"brave": provider.config},
        cache=SearchCache(redis=redis),
        ledger=QuotaLedger(redis=redis),
    )
    for i in range(10):
        assert await router.search(f"q{i}", count=3)
    assert router.ledger.redis_failures > 0, "should have noticed Redis dying"


async def test_ddg_is_never_first_and_never_alone():
    ddg = FakeProvider("ddg", config=ProviderConfig(name="ddg", last_resort=True))
    brave = FakeProvider("brave", config=ProviderConfig(name="brave", per_minute=500))
    router = build([brave, ddg])
    for i in range(20):
        await router.search(f"unique query {i}", count=3)
    assert brave.calls == 20
    assert ddg.calls == 0, "last-resort provider was used while another was healthy"
    assert router.telemetry.share("ddg") < 0.05


async def test_query_budget_is_raised_not_silently_drained():
    provider = FakeProvider("brave")
    router = build([provider], max_queries=5)
    for i in range(5):
        await router.search(f"q{i}", count=3)
    with pytest.raises(QueryBudgetExceeded):
        await router.search("one too many", count=3)


async def test_empty_results_are_negative_cached():
    provider = FakeProvider("brave", results=0)
    router = build([provider])
    assert await router.search("nothing sells this", count=3) == []
    assert await router.search("nothing sells this", count=3) == []
    assert provider.calls == 1, "an empty answer was re-asked upstream"
