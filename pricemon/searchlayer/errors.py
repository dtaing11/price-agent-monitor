"""The only failures an adapter is allowed to raise.

Every provider library has its own exception vocabulary. Letting those escape
means the router has to know about all of them, so each adapter translates into
exactly these four - anything else is a bug in the adapter.
"""

from __future__ import annotations

from datetime import datetime


class SearchLayerError(Exception):
    """Base for everything this package raises."""


class ProviderError(SearchLayerError):
    """A single provider failed. The router may still succeed elsewhere."""

    def __init__(self, provider: str, message: str = ""):
        self.provider = provider
        super().__init__(f"{provider}: {message}" if message else provider)


class RateLimited(ProviderError):
    """Quota is spent. `retry_after` is seconds, when the provider says."""

    def __init__(
        self, provider: str, retry_after: float | None = None, message: str = ""
    ):
        self.retry_after = retry_after
        super().__init__(
            provider, message or f"rate limited (retry after {retry_after}s)"
        )


class Upstream5xx(ProviderError):
    """The provider is broken right now, which is not our accounting problem."""


class ProviderTimeout(ProviderError):
    """No answer inside the deadline."""


class MalformedResponse(ProviderError):
    """A 200 whose body was not what the contract promises."""


class AllProvidersExhausted(SearchLayerError):
    """Every provider is out of budget, broken, or open-circuited.

    Carries the earliest moment any of them frees up, so a caller can decide
    between waiting and giving up rather than guessing.
    """

    def __init__(
        self, reset_at: datetime | None = None, tried: list[str] | None = None
    ):
        self.reset_at = reset_at
        self.tried = tried or []
        when = f"; earliest reset {reset_at.isoformat()}" if reset_at else ""
        super().__init__(
            f"no search provider available (tried: {', '.join(self.tried)}){when}"
        )


class QueryBudgetExceeded(SearchLayerError):
    """A task asked for more searches than it was allotted.

    Raised rather than silently served, because a loop that wants 400 searches
    is a bug worth surfacing, not a quota to drain.
    """
