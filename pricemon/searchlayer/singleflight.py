"""One call for N identical concurrent questions.

Fifty agents asking the same thing at once is fifty times the quota for one
answer. A registry of in-flight futures collapses them: the first caller does
the work, the rest await its result - including its exception, so a failure is
not silently swallowed by forty-nine waiters.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


class SingleFlight:
    def __init__(self) -> None:
        self._inflight: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()
        self.coalesced = 0  # how many calls this saved, for observability

    async def do(self, key: str, fn: Callable[[], Awaitable[T]]) -> T:
        async with self._lock:
            existing = self._inflight.get(key)
            if existing is not None:
                self.coalesced += 1
                waiter: asyncio.Future = existing
            else:
                waiter = asyncio.get_running_loop().create_future()
                self._inflight[key] = waiter
        if existing is not None:
            return await asyncio.shield(waiter)

        try:
            result = await fn()
        except BaseException as exc:
            async with self._lock:
                self._inflight.pop(key, None)
            if not waiter.done():
                waiter.set_exception(exc)
            if waiter.done():
                waiter.exception()  # mark retrieved; nobody must consume it
            raise
        else:
            async with self._lock:
                self._inflight.pop(key, None)
            if not waiter.done():
                waiter.set_result(result)
            return result

    @property
    def in_flight(self) -> int:
        return len(self._inflight)
