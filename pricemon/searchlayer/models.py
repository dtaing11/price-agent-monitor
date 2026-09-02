"""The shapes that cross the boundary between callers, router and providers."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SearchResult(BaseModel):
    """One hit, from whichever provider found it."""

    model_config = ConfigDict(frozen=True)

    url: str
    title: str = ""
    snippet: str = ""
    published_at: datetime | None = None
    rank: int = 0
    provider: str = ""

    @field_validator("url")
    @classmethod
    def _must_be_http(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError(f"not an http(s) url: {value!r}")
        return value


class ProviderHealth(BaseModel):
    """What a provider reports about itself, for routing decisions."""

    name: str
    configured: bool = False  # has the credentials it needs
    healthy: bool = True  # circuit closed, no recent hard failures
    remaining: int | None = None  # calls left in the tightest window
    reset_at: datetime | None = None
    note: str = ""


class ProviderConfig(BaseModel):
    """Everything the router needs to know to spend a provider's quota."""

    name: str
    tier: int = 0  # 0 = free, higher = costs money
    enabled: bool = True
    last_resort: bool = False  # never chosen first, never used alone
    api_keys: list[str] = Field(default_factory=list)
    per_second: float | None = None
    per_minute: int | None = None
    per_day: int | None = None
    per_month: int | None = None
    endpoint: str | None = None  # for self-hosted providers
    timeout: float = 12.0
    # Documented quota is the cliff, not the target.
    headroom: float = 0.8


@runtime_checkable
class SearchProvider(Protocol):
    """What every adapter must offer, and nothing more."""

    name: str

    async def search(
        self, query: str, count: int = 10, lang: str = "en"
    ) -> list[SearchResult]: ...

    async def health(self) -> ProviderHealth: ...
