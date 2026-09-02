"""Counters, because a quota system you cannot see is a quota system you trust
blindly. Cache hit rate is instrumented from the first commit, since it is the
number that says whether any of the rest was necessary."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Telemetry:
    calls: Counter = field(default_factory=Counter)  # per provider
    errors: Counter = field(default_factory=Counter)  # per provider:error
    skipped: Counter = field(default_factory=Counter)  # per provider:reason
    served_from: Counter = field(default_factory=Counter)  # cache / provider / stale
    coalesced: int = 0

    def record_call(self, provider: str) -> None:
        self.calls[provider] += 1

    def record_error(self, provider: str, kind: str) -> None:
        self.errors[f"{provider}:{kind}"] += 1

    def record_skip(self, provider: str, reason: str) -> None:
        self.skipped[f"{provider}:{reason}"] += 1

    def record_served(self, source: str) -> None:
        self.served_from[source] += 1

    @property
    def total_calls(self) -> int:
        return sum(self.calls.values())

    def share(self, provider: str) -> float:
        return 0.0 if not self.total_calls else self.calls[provider] / self.total_calls

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": dict(self.calls),
            "total_calls": self.total_calls,
            "errors": dict(self.errors),
            "skipped": dict(self.skipped),
            "served_from": dict(self.served_from),
            "coalesced": self.coalesced,
        }
