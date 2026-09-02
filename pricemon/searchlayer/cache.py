"""Two tiers of cache, plus the ones people forget: negative and stale.

Tier one is in-process, so a hot loop never leaves the interpreter. Tier two is
Redis, so a restart does not replay the whole trace cold and so several workers
share one answer. Redis is optional throughout: when it is missing or dies
mid-run, everything keeps working from the in-process tier.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any

from cachetools import TTLCache  # type: ignore[import-untyped]

from .models import SearchResult
from .normalize import cache_key, tokens

logger = logging.getLogger(__name__)

EMPTY = "\x00empty"  # distinguishes "known to have no results" from "not cached"


@dataclass
class CacheStats:
    """Hit rate is the number that tells you whether any of this is working."""

    hits_memory: int = 0
    hits_redis: int = 0
    hits_semantic: int = 0
    hits_stale: int = 0
    misses: int = 0
    negative_hits: int = 0

    @property
    def hits(self) -> int:
        return self.hits_memory + self.hits_redis + self.hits_semantic

    @property
    def lookups(self) -> int:
        return self.hits + self.misses + self.negative_hits

    @property
    def hit_rate(self) -> float:
        return (
            0.0 if not self.lookups else (self.hits + self.negative_hits) / self.lookups
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "lookups": self.lookups,
            "hit_rate": round(self.hit_rate, 3),
            "memory": self.hits_memory,
            "redis": self.hits_redis,
            "semantic": self.hits_semantic,
            "negative": self.negative_hits,
            "stale_served": self.hits_stale,
            "misses": self.misses,
        }


def _vector(query: str) -> dict[str, float]:
    """A bag-of-words vector for the semantic tier.

    A real embedding model would read meaning; this reads vocabulary overlap,
    which is what actually separates an agent's paraphrases of one question
    ("cheapest sony xm5" / "sony xm5 lowest price") from a different question.
    It needs no model, no API call and no key, and `SemanticIndex` takes an
    embedder if you want the real thing.
    """
    counts: dict[str, float] = {}
    for token in tokens(query):
        counts[token] = counts.get(token, 0.0) + 1.0
    norm = math.sqrt(sum(v * v for v in counts.values())) or 1.0
    return {k: v / norm for k, v in counts.items()}


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(weight * b.get(token, 0.0) for token, weight in a.items())


@dataclass
class SemanticIndex:
    """Recently cached queries, searchable by similarity.

    Bounded on purpose: this is a cache accelerator, not a vector database.
    """

    threshold: float = 0.95
    capacity: int = 512
    entries: list[tuple[str, dict[str, float]]] = field(default_factory=list)

    def add(self, query: str, key: str) -> None:
        self.entries.append((key, _vector(query)))
        if len(self.entries) > self.capacity:
            del self.entries[: len(self.entries) - self.capacity]

    def find(self, query: str) -> str | None:
        target = _vector(query)
        best_key, best_score = None, 0.0
        for key, vector in reversed(self.entries):
            score = cosine(target, vector)
            if score > best_score:
                best_key, best_score = key, score
        return best_key if best_score >= self.threshold else None


class SearchCache:
    """Memory over Redis, with negative entries and a stale tier."""

    def __init__(
        self,
        redis=None,
        ttl: float = 3600.0,
        negative_ttl: float = 120.0,
        stale_ttl: float = 86400.0,
        maxsize: int = 2048,
        semantic: bool = True,
        namespace: str = "pricemon:search",
    ):
        self.redis = redis
        self.ttl = ttl
        self.negative_ttl = negative_ttl
        self.stale_ttl = stale_ttl
        self.namespace = namespace
        self.memory: TTLCache = TTLCache(maxsize=maxsize, ttl=ttl)
        # Kept far longer than the fresh tier, to answer with something rather
        # than nothing when every provider is exhausted.
        self.stale: TTLCache = TTLCache(maxsize=maxsize, ttl=stale_ttl)
        self.semantic = SemanticIndex() if semantic else None
        self.stats = CacheStats()

    # -- plumbing ---------------------------------------------------------
    def _rkey(self, key: str) -> str:
        return f"{self.namespace}:{key}"

    @staticmethod
    def _dump(results: list[SearchResult]) -> str:
        return json.dumps([r.model_dump(mode="json") for r in results])

    @staticmethod
    def _load(raw: str) -> list[SearchResult]:
        return [SearchResult.model_validate(item) for item in json.loads(raw)]

    async def _redis_get(self, key: str) -> str | None:
        if self.redis is None:
            return None
        try:
            value = await self.redis.get(self._rkey(key))
        except Exception as exc:  # noqa: BLE001 - Redis is optional, always
            logger.debug("redis get failed, continuing without it: %s", exc)
            return None
        return value.decode() if isinstance(value, bytes) else value

    async def _redis_set(self, key: str, value: str, ttl: float) -> None:
        if self.redis is None:
            return
        try:
            await self.redis.set(self._rkey(key), value, ex=int(max(ttl, 1)))
        except Exception as exc:  # noqa: BLE001
            logger.debug("redis set failed, continuing without it: %s", exc)

    # -- the actual cache -------------------------------------------------
    async def get(self, query: str, count: int = 10, lang: str = "en"):
        """Fresh results, [] for a known-empty query, or None for a miss."""
        key = cache_key(query, count, lang)

        hit = self.memory.get(key)
        if hit is not None:
            self.stats.hits_memory += 1
            if hit == EMPTY:
                self.stats.hits_memory -= 1
                self.stats.negative_hits += 1
                return []
            return hit

        raw = await self._redis_get(key)
        if raw is not None:
            if raw == EMPTY:
                self.memory[key] = EMPTY
                self.stats.negative_hits += 1
                return []
            try:
                results = self._load(raw)
            except Exception:  # noqa: BLE001 - a poisoned entry is just a miss
                results = None
            if results is not None:
                self.memory[key] = results
                self.stats.hits_redis += 1
                return results

        if self.semantic is not None:
            near = self.semantic.find(query)
            if near is not None and near != key:
                hit = self.memory.get(near)
                if hit is not None and hit != EMPTY:
                    self.stats.hits_semantic += 1
                    return hit

        self.stats.misses += 1
        return None

    async def set(
        self, query: str, results: list[SearchResult], count: int = 10, lang: str = "en"
    ) -> None:
        key = cache_key(query, count, lang)
        if results:
            self.memory[key] = results
            self.stale[key] = (results, time.time())
            await self._redis_set(key, self._dump(results), self.ttl)
            if self.semantic is not None:
                self.semantic.add(query, key)
        else:
            # Remember the emptiness too, briefly. Re-asking a question every
            # provider just answered with nothing is pure quota burn.
            self.memory[key] = EMPTY
            await self._redis_set(key, EMPTY, self.negative_ttl)

    async def get_stale(self, query: str, count: int = 10, lang: str = "en"):
        """Something rather than nothing, when everything is exhausted."""
        entry = self.stale.get(cache_key(query, count, lang))
        if entry is None:
            return None
        results, stored_at = entry
        self.stats.hits_stale += 1
        return results, stored_at
