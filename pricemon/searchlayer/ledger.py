"""The quota ledger: what we have spent, and what we may still spend.

The point of this module is that a 429 should never happen. A provider's limits
are known up front, so calls are counted *before* they go out and refused when
a window is full - instead of firing and finding out. Counts live in Redis so
every worker spends from the same budget, and fall back to in-process counting
when Redis is absent or dies mid-run.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .models import ProviderConfig

logger = logging.getLogger(__name__)

# name -> seconds. Rolling, so "per day" means the last 86400s, not since midnight.
WINDOWS: dict[str, int] = {
    "second": 1,
    "minute": 60,
    "day": 86_400,
    "month": 2_592_000,
}


@dataclass
class Verdict:
    """Whether a call may go out, and if not, when to look again."""

    allowed: bool
    window: str = ""
    retry_after: float = 0.0
    remaining: int | None = None

    @property
    def reset_at(self) -> datetime | None:
        if self.allowed or not self.retry_after:
            return None
        return datetime.now(timezone.utc) + timedelta(seconds=self.retry_after)


def budget_for(config: ProviderConfig, window: str) -> int | None:
    """The cap we hold ourselves to - a margin under the documented one."""
    raw = {
        "second": config.per_second,
        "minute": config.per_minute,
        "day": config.per_day,
        "month": config.per_month,
    }[window]
    if raw is None:
        return None
    return max(1, int(raw * config.headroom))


class QuotaLedger:
    """Rolling per-window call counts, shared through Redis when available."""

    def __init__(self, redis=None, namespace: str = "pricemon:quota"):
        self.redis = redis
        self.namespace = namespace
        self._local: dict[str, deque[float]] = defaultdict(deque)
        self._overrides: dict[
            str, tuple[int, float]
        ] = {}  # provider -> (remaining, reset_ts)
        self._lock = asyncio.Lock()
        self.redis_failures = 0

    def _key(self, provider: str, key_id: str, window: str) -> str:
        return f"{self.namespace}:{provider}:{key_id}:{window}"

    def _local_key(self, provider: str, key_id: str) -> str:
        return f"{provider}:{key_id}"

    # -- counting ---------------------------------------------------------
    async def _count(self, provider: str, key_id: str, window: str, now: float) -> int:
        span = WINDOWS[window]
        if self.redis is not None:
            try:
                key = self._key(provider, key_id, window)
                await self.redis.zremrangebyscore(key, 0, now - span)
                return int(await self.redis.zcard(key))
            except Exception as exc:  # noqa: BLE001 - degrade, never fail
                self.redis_failures += 1
                logger.debug("ledger falling back to memory: %s", exc)
        marks = self._local[self._local_key(provider, key_id)]
        while marks and marks[0] < now - span:
            marks.popleft()
        return len(marks)

    async def check(self, config: ProviderConfig, key_id: str = "default") -> Verdict:
        """May this provider be called right now?"""
        now = time.time()

        override = self._overrides.get(self._local_key(config.name, key_id))
        if override is not None:
            remaining, reset_ts = override
            if remaining <= 0:
                if reset_ts > now:
                    return Verdict(False, "provider", max(0.0, reset_ts - now), 0)
                self._overrides.pop(self._local_key(config.name, key_id), None)

        for window in ("second", "minute", "day", "month"):
            cap = budget_for(config, window)
            if cap is None:
                continue
            used = await self._count(config.name, key_id, window, now)
            if used >= cap:
                return Verdict(False, window, float(WINDOWS[window]), 0)
        return Verdict(True)

    async def record(self, provider: str, key_id: str = "default") -> None:
        """Count a call as spent, before its result is known."""
        now = time.time()
        if self.redis is not None:
            try:
                pipe = self.redis.pipeline()
                for window, span in WINDOWS.items():
                    key = self._key(provider, key_id, window)
                    pipe.zadd(key, {f"{now}:{id(object())}": now})
                    pipe.expire(key, span + 60)
                await pipe.execute()
                return
            except Exception as exc:  # noqa: BLE001
                self.redis_failures += 1
                logger.debug("ledger falling back to memory: %s", exc)
        self._local[self._local_key(provider, key_id)].append(now)

    async def remaining(
        self,
        config: ProviderConfig,
        key_id: str = "default",
        for_routing: bool = False,
    ) -> int | None:
        """Calls left in the tightest configured window.

        For routing, the per-second window is ignored: it describes how fast a
        provider may be called, not how much of it is left, and the pacer
        already handles that. Including it made a provider with a tight burst
        limit look nearly exhausted and quietly starved it of traffic.
        """
        now = time.time()
        left: int | None = None
        windows = [w for w in WINDOWS if not (for_routing and w == "second")]
        for window in windows:
            cap = budget_for(config, window)
            if cap is None:
                continue
            used = await self._count(config.name, key_id, window, now)
            free = max(0, cap - used)
            left = free if left is None else min(left, free)
        override = self._overrides.get(self._local_key(config.name, key_id))
        if override is not None and override[1] > now:
            left = override[0] if left is None else min(left, override[0])
        return left

    # -- learning from the provider ---------------------------------------
    def reconcile(self, provider: str, headers, key_id: str = "default") -> None:
        """Believe the provider over our own arithmetic.

        Every response carries the truth about what is left; our counters are
        only an estimate between responses.
        """
        if not headers:
            return
        lowered = {str(k).lower(): str(v) for k, v in dict(headers).items()}

        remaining = None
        for name in (
            "x-ratelimit-remaining",
            "ratelimit-remaining",
            "x-rate-limit-remaining",
        ):
            if name in lowered:
                try:
                    remaining = int(float(lowered[name].split(",")[0].strip()))
                except ValueError:
                    remaining = None
                break

        reset_ts = None
        for name in ("x-ratelimit-reset", "ratelimit-reset", "x-rate-limit-reset"):
            if name in lowered:
                reset_ts = _parse_reset(lowered[name])
                break
        if reset_ts is None and "retry-after" in lowered:
            reset_ts = _parse_reset(lowered["retry-after"])

        if remaining is not None:
            self._overrides[self._local_key(provider, key_id)] = (
                remaining,
                reset_ts or (time.time() + 60),
            )

    def note_rate_limited(
        self, provider: str, retry_after: float | None, key_id: str = "default"
    ) -> None:
        """A 429 slipped through: stop calling until it resets."""
        self._overrides[self._local_key(provider, key_id)] = (
            0,
            time.time() + (retry_after or 60.0),
        )


def _parse_reset(value: str) -> float | None:
    """Reset headers come as seconds-from-now, or an epoch, or a date."""
    value = value.strip()
    try:
        number = float(value)
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime

            return parsedate_to_datetime(value).timestamp()
        except Exception:  # noqa: BLE001
            return None
    now = time.time()
    # Anything that looks like an epoch is one; smaller numbers are a delay.
    return number if number > now - 86_400 else now + number
