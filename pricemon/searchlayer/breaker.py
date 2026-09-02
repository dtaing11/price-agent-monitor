"""Stop asking a provider that is currently saying no.

Two consecutive rate limits is enough evidence: something is wrong with our
accounting for that provider, and continuing to call it only makes the reset
later. The cooldown follows the provider's own reset when it gave one, because
guessing an interval is how thundering herds happen.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class CircuitBreaker:
    """Closed, open, or letting a single probe through."""

    threshold: int = 2
    base_cooldown: float = 30.0
    max_cooldown: float = 900.0

    failures: int = 0
    opened_at: float = 0.0
    cooldown: float = 0.0
    trips: int = 0

    @property
    def is_open(self) -> bool:
        if not self.opened_at:
            return False
        # Once the cooldown has elapsed the circuit is closed again, and the
        # next call through is the probe.
        return time.time() - self.opened_at < self.cooldown

    @property
    def reset_at(self) -> datetime | None:
        if not self.is_open:
            return None
        return datetime.fromtimestamp(self.opened_at + self.cooldown, tz=timezone.utc)

    def record_success(self) -> None:
        """One good answer closes it - a probe that works means recovery."""
        self.failures = 0
        self.opened_at = 0.0
        self.cooldown = 0.0

    def record_rate_limited(self, retry_after: float | None = None) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.trips += 1
            self.opened_at = time.time()
            self.cooldown = min(
                self.max_cooldown,
                retry_after
                if retry_after
                else self.base_cooldown * (2 ** (self.trips - 1)),
            )

    def record_failure(self, retry_after: float | None = None) -> None:
        """A 5xx or timeout counts, but more forgivingly than a rate limit."""
        self.failures += 1
        if self.failures >= self.threshold + 1:
            self.trips += 1
            self.opened_at = time.time()
            self.cooldown = min(self.max_cooldown, retry_after or self.base_cooldown)
