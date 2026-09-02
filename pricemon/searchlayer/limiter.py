"""Pacing and concurrency: the ledger says *whether*, this says *how fast*.

Two mechanisms. A token-bucket limiter per provider holds the send rate under
the documented quota, so bursts do not arrive as a spike. An AIMD controller
adjusts how many calls may be in flight: it grows slowly while everything
succeeds and halves immediately on a 429 or timeout, which is the behaviour
that keeps a shared quota stable instead of oscillating.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from aiolimiter import AsyncLimiter

from .ledger import budget_for
from .models import ProviderConfig


def limiter_for(config: ProviderConfig) -> AsyncLimiter | None:
    """A token bucket set to our self-imposed cap, not the documented one."""
    per_second = budget_for(config, "second")
    per_minute = budget_for(config, "minute")
    if per_second is not None:
        return AsyncLimiter(max(1, per_second), 1.0)
    if per_minute is not None:
        return AsyncLimiter(max(1, per_minute), 60.0)
    return None


@dataclass
class AdaptiveConcurrency:
    """Additive increase, multiplicative decrease, per provider.

    Success is cheap evidence and moves the cap by one; a rate limit is
    expensive evidence and halves it. Recovery is therefore slow and backing
    off is instant, which is the right asymmetry when the cost of being wrong
    is someone else's quota.
    """

    initial: int = 4
    minimum: int = 1
    maximum: int = 16
    success_run: int = 5  # successes needed before widening

    limit: int = field(init=False)
    _successes: int = field(default=0, init=False)
    _sem: asyncio.Semaphore = field(init=False)
    _lock: asyncio.Lock = field(init=False)

    def __post_init__(self) -> None:
        self.limit = self.initial
        self._sem = asyncio.Semaphore(self.initial)
        self._lock = asyncio.Lock()

    async def __aenter__(self):
        await self._sem.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._sem.release()
        return False

    async def on_success(self) -> None:
        async with self._lock:
            self._successes += 1
            if self._successes >= self.success_run and self.limit < self.maximum:
                self._successes = 0
                self.limit += 1
                self._sem.release()  # widen by one

    async def on_throttled(self) -> None:
        """Halve the cap. Permits already held drain naturally."""
        async with self._lock:
            self._successes = 0
            new_limit = max(self.minimum, self.limit // 2)
            to_remove = self.limit - new_limit
            self.limit = new_limit
            for _ in range(to_remove):
                # Take a permit out of circulation without blocking: if none is
                # free, the shrink lands as in-flight calls finish.
                if not self._sem.locked():
                    try:
                        self._sem._value = max(0, self._sem._value - 1)
                    except AttributeError:  # pragma: no cover
                        pass


class ProviderPacer:
    """Everything that decides when one provider's next call may leave."""

    def __init__(self, config: ProviderConfig):
        self.config = config
        self.bucket = limiter_for(config)
        self.concurrency = AdaptiveConcurrency()

    async def acquire(self) -> None:
        if self.bucket is not None:
            await self.bucket.acquire()

    @property
    def in_flight_limit(self) -> int:
        return self.concurrency.limit
