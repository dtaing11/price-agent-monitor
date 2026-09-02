"""The router: spend the cheapest available budget, and never overspend.

Order of operations for one search, and the reasoning behind it:

  cache            answering without a call is always cheapest
  single-flight    identical concurrent questions collapse into one call
  ledger           a provider is only called while its budget says yes, which
                   is what replaces trying and reading the 429
  pacing + AIMD    hold the send rate under the cap, and back off hard on any
                   sign of throttling
  breaker          stop asking a provider that has said no twice
  degrade          stale cache, or a clear exhaustion error carrying the
                   earliest reset - never a rate-limit error from upstream
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from .breaker import CircuitBreaker
from .cache import SearchCache
from .errors import (
    AllProvidersExhausted,
    MalformedResponse,
    ProviderTimeout,
    QueryBudgetExceeded,
    RateLimited,
    Upstream5xx,
)
from .fusion import reciprocal_rank_fusion
from .ledger import QuotaLedger
from .limiter import ProviderPacer
from .models import ProviderConfig, ProviderHealth, SearchResult
from .normalize import cache_key
from .observability import Telemetry
from .singleflight import SingleFlight

logger = logging.getLogger(__name__)


class SearchRouter:
    """One entry point: `search()`. Everything else is bookkeeping."""

    def __init__(
        self,
        providers: list,
        configs: dict[str, ProviderConfig] | None = None,
        cache: SearchCache | None = None,
        ledger: QuotaLedger | None = None,
        serve_stale: bool = True,
        max_queries: int | None = None,
        fanout: int = 1,
    ):
        self.providers = {p.name: p for p in providers}
        self.configs = configs or {
            p.name: getattr(p, "config", ProviderConfig(name=p.name)) for p in providers
        }
        self.cache = cache or SearchCache()
        self.ledger = ledger or QuotaLedger()
        self.telemetry = Telemetry()
        self.singleflight = SingleFlight()
        self.serve_stale = serve_stale
        self.max_queries = max_queries
        self.fanout = max(1, fanout)
        self.queries_used = 0

        self.breakers = {name: CircuitBreaker() for name in self.providers}
        self.pacers = {
            name: ProviderPacer(self.configs.get(name, ProviderConfig(name=name)))
            for name in self.providers
        }

    # -- selection --------------------------------------------------------
    async def _order_providers(self) -> list[str]:
        """Cheapest tier first, and within a tier, whoever has most left.

        Fixed priority drains one provider then falls over; weighting by what
        remains keeps every budget alive for longer.
        """
        scored: list[tuple[int, float, str]] = []
        for name, provider in self.providers.items():
            config = self.configs.get(name, ProviderConfig(name=name))
            if not config.enabled or not getattr(provider, "configured", True):
                continue
            if self.breakers[name].is_open:
                self.telemetry.record_skip(name, "breaker_open")
                continue
            verdict = await self.ledger.check(config)
            if not verdict.allowed:
                self.telemetry.record_skip(name, f"no_budget_{verdict.window}")
                if verdict.window == "provider":
                    # Refused because the provider itself said it was rate
                    # limited. Every attempt that lands here is a consecutive
                    # refusal, so the breaker learns even though no call was
                    # spent - otherwise the ledger's efficiency at blocking
                    # calls would keep the circuit closed indefinitely.
                    self.breakers[name].record_rate_limited(verdict.retry_after)
                continue
            left = await self.ledger.remaining(config, for_routing=True)
            # Unmetered providers sort as "plenty left" but stay in their tier.
            headroom = float(left if left is not None else 10_000)
            scored.append((config.tier, -headroom, name))

        scored.sort()
        ordered = [name for _tier, _room, name in scored]

        # Last-resort providers are never first, and never the only one tried.
        last = [
            n
            for n in ordered
            if self.configs.get(n, ProviderConfig(name=n)).last_resort
        ]
        rest = [n for n in ordered if n not in last]
        return rest + last if rest else last

    # -- one provider call ------------------------------------------------
    async def _call(self, name: str, query: str, count: int, lang: str):
        provider = self.providers[name]
        config = self.configs.get(name, ProviderConfig(name=name))
        pacer = self.pacers[name]

        verdict = await self.ledger.check(config)
        if not verdict.allowed:
            self.telemetry.record_skip(name, f"ledger_{verdict.window}")
            if verdict.window == "provider":
                # The ledger is refusing because this provider already told us
                # it was rate limited. That is a second consecutive refusal,
                # and the breaker should learn it even though we spent no call
                # finding out - otherwise the ledger's own efficiency would
                # keep the circuit closed forever.
                self.breakers[name].record_rate_limited(verdict.retry_after)
            return None

        await pacer.acquire()
        async with pacer.concurrency:
            await self.ledger.record(name)
            self.telemetry.record_call(name)
            try:
                results = await provider.search(query, count=count, lang=lang)
            except RateLimited as exc:
                # Should not happen once the ledger is right; when it does, it
                # is treated as new information about the budget.
                self.telemetry.record_error(name, "rate_limited")
                self.ledger.note_rate_limited(name, exc.retry_after)
                self.breakers[name].record_rate_limited(exc.retry_after)
                await pacer.concurrency.on_throttled()
                return None
            except ProviderTimeout:
                self.telemetry.record_error(name, "timeout")
                self.breakers[name].record_failure()
                await pacer.concurrency.on_throttled()
                return None
            except (Upstream5xx, MalformedResponse) as exc:
                self.telemetry.record_error(name, type(exc).__name__.lower())
                self.breakers[name].record_failure()
                return None

            self.ledger.reconcile(name, getattr(provider, "last_headers", None))
            self.breakers[name].record_success()
            await pacer.concurrency.on_success()
            return results

    # -- public -----------------------------------------------------------
    async def search(
        self, query: str, count: int = 10, lang: str = "en"
    ) -> list[SearchResult]:
        """Results for a query, from cache or the cheapest available budget."""
        cached = await self.cache.get(query, count, lang)
        if cached is not None:
            self.telemetry.record_served("cache")
            return cached

        if self.max_queries is not None and self.queries_used >= self.max_queries:
            raise QueryBudgetExceeded(
                f"this task was allowed {self.max_queries} searches and has used them; "
                "raise the cap deliberately rather than draining quota"
            )

        key = cache_key(query, count, lang)
        before = self.singleflight.coalesced
        results = await self.singleflight.do(
            key, lambda: self._search_uncached(query, count, lang)
        )
        self.telemetry.coalesced += self.singleflight.coalesced - before
        return results

    async def _search_uncached(
        self, query: str, count: int, lang: str
    ) -> list[SearchResult]:
        self.queries_used += 1
        order = await self._order_providers()
        rankings: list[list[SearchResult]] = []
        tried: list[str] = []

        answered_empty = False
        for name in order:
            tried.append(name)
            results = await self._call(name, query, count, lang)
            if results is None:
                continue  # no answer at all - throttled, broken, or skipped
            if results:
                rankings.append(results)
                if len(rankings) >= self.fanout:
                    break
            else:
                # A provider that successfully answered "nothing" is a real
                # result, and quite different from one that never answered.
                answered_empty = True

        if rankings:
            merged = reciprocal_rank_fusion(rankings, limit=count)
            await self.cache.set(query, merged, count, lang)
            self.telemetry.record_served("provider")
            return merged

        # Nobody could answer. Prefer something old over an exception.
        if self.serve_stale:
            stale = await self.cache.get_stale(query, count, lang)
            if stale is not None:
                results, _stored_at = stale
                self.telemetry.record_served("stale")
                return [
                    r.model_copy(update={"provider": f"{r.provider} (stale)"})
                    for r in results
                ]

        # A provider genuinely answered "nothing sells this". Remember it
        # briefly rather than re-asking every provider the same dead question.
        if answered_empty:
            await self.cache.set(query, [], count, lang)
            self.telemetry.record_served("empty")
            return []

        raise AllProvidersExhausted(await self._earliest_reset(), tried)

    async def _earliest_reset(self) -> datetime | None:
        soonest: datetime | None = None
        for name in self.providers:
            reset = self.breakers[name].reset_at
            if reset and (soonest is None or reset < soonest):
                soonest = reset
        return soonest or datetime.now(timezone.utc)

    async def health(self) -> list[ProviderHealth]:
        out = []
        for name, provider in self.providers.items():
            config = self.configs.get(name, ProviderConfig(name=name))
            health = await provider.health()
            out.append(
                health.model_copy(
                    update={
                        "healthy": not self.breakers[name].is_open,
                        "remaining": await self.ledger.remaining(config),
                        "reset_at": self.breakers[name].reset_at,
                    }
                )
            )
        return out

    def report(self) -> dict:
        return {
            "cache": self.cache.stats.as_dict(),
            "telemetry": self.telemetry.as_dict(),
            "queries_used": self.queries_used,
            "redis_failures": self.ledger.redis_failures,
        }


async def gather_searches(router: SearchRouter, queries: list[str], count: int = 10):
    """Run several searches concurrently through one router."""
    return await asyncio.gather(
        *(router.search(q, count=count) for q in queries), return_exceptions=True
    )
