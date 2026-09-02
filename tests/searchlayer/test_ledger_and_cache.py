"""The ledger against a real (fake) Redis, and the cache tiers."""

from __future__ import annotations

import asyncio

import pytest
from fakeredis import aioredis as fake_aioredis

from pricemon.searchlayer.cache import SearchCache, cosine
from pricemon.searchlayer.ledger import QuotaLedger, budget_for
from pricemon.searchlayer.models import ProviderConfig, SearchResult

pytestmark = pytest.mark.asyncio


@pytest.fixture
def redis():
    return fake_aioredis.FakeRedis()


async def test_budget_holds_back_from_the_documented_cliff():
    config = ProviderConfig(name="brave", per_minute=100)
    assert budget_for(config, "minute") == 80, "should aim at 80% of documented quota"
    assert budget_for(config, "day") is None


async def test_window_is_enforced_before_the_call(redis):
    ledger = QuotaLedger(redis=redis)
    config = ProviderConfig(name="brave", per_minute=10)  # 8 after headroom
    for _ in range(8):
        assert (await ledger.check(config)).allowed
        await ledger.record("brave")
    verdict = await ledger.check(config)
    assert not verdict.allowed and verdict.window == "minute"
    assert await ledger.remaining(config) == 0


async def test_workers_share_one_budget_through_redis(redis):
    config = ProviderConfig(name="brave", per_minute=10)  # 8 usable
    worker_a = QuotaLedger(redis=redis)
    worker_b = QuotaLedger(redis=redis)

    for _ in range(8):
        await worker_a.record("brave")
    # The second worker must see the first worker's spending.
    assert not (await worker_b.check(config)).allowed


async def test_ledger_keeps_working_when_redis_is_gone():
    class DeadRedis:
        async def zremrangebyscore(self, *a, **k):
            raise ConnectionError("gone")

        async def zcard(self, *a, **k):
            raise ConnectionError("gone")

        def pipeline(self):
            raise ConnectionError("gone")

    ledger = QuotaLedger(redis=DeadRedis())
    config = ProviderConfig(name="brave", per_minute=10)
    for _ in range(8):
        assert (await ledger.check(config)).allowed
        await ledger.record("brave")
    assert not (await ledger.check(config)).allowed, "in-process limits must still hold"
    assert ledger.redis_failures > 0


async def test_provider_headers_override_our_arithmetic():
    ledger = QuotaLedger()
    config = ProviderConfig(name="brave", per_minute=1000)
    assert await ledger.remaining(config) == 800
    ledger.reconcile("brave", {"X-RateLimit-Remaining": "2", "X-RateLimit-Reset": "30"})
    assert await ledger.remaining(config) == 2


async def test_a_429_stops_further_calls_until_reset():
    ledger = QuotaLedger()
    config = ProviderConfig(name="ddg")
    ledger.note_rate_limited("ddg", retry_after=45)
    verdict = await ledger.check(config)
    assert not verdict.allowed
    assert verdict.window == "provider"
    assert 40 < verdict.retry_after <= 45
    assert verdict.reset_at is not None


async def test_cache_survives_a_restart_through_redis(redis):
    results = [SearchResult(url="https://a.test/p", provider="brave", rank=1)]
    first = SearchCache(redis=redis)
    await first.set("thunderbolt dock", results)

    # A new process: cold memory, warm Redis.
    second = SearchCache(redis=redis)
    got = await second.get("thunderbolt dock")
    assert got and got[0].url == "https://a.test/p"
    assert second.stats.hits_redis == 1


async def test_semantic_tier_serves_a_paraphrase():
    cache = SearchCache()
    results = [SearchResult(url="https://a.test/p", provider="brave", rank=1)]
    await cache.set("sony wh-1000xm5 wireless headphones", results)

    # Same question, different wording and word order.
    got = await cache.get("wireless headphones sony wh-1000xm5")
    assert got is not None and got[0].url == "https://a.test/p"


async def test_semantic_tier_does_not_confuse_different_questions():
    cache = SearchCache()
    await cache.set(
        "thunderbolt dock", [SearchResult(url="https://a.test/p", provider="b")]
    )
    assert await cache.get("air fryer") is None
    assert cosine({"a": 1.0}, {"b": 1.0}) == 0.0


async def test_negative_entries_expire_separately(redis):
    cache = SearchCache(redis=redis, negative_ttl=60)
    await cache.set("nothing sells this", [])
    assert await cache.get("nothing sells this") == []
    assert cache.stats.negative_hits == 1


async def test_concurrent_cache_writes_do_not_corrupt(redis):
    cache = SearchCache(redis=redis)
    await asyncio.gather(
        *(
            cache.set(
                f"query {i}", [SearchResult(url=f"https://a.test/{i}", provider="b")]
            )
            for i in range(50)
        )
    )
    for i in range(50):
        got = await cache.get(f"query {i}")
        assert got and got[0].url == f"https://a.test/{i}"
