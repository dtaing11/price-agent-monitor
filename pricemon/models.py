"""Core data types shared across the agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def utcnow() -> str:
    """ISO-8601 UTC timestamp, second resolution."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Product:
    """A page we watch."""

    name: str
    url: str
    id: int | None = None
    title: str | None = None  # the product's own name, as the page states it
    image: str | None = None  # product photo URL, for the list view
    selector: str | None = None  # user-pinned, always tried first
    learned_selector: str | None = None  # discovered by the LLM, self-healing
    target_price: float | None = None
    currency: str | None = None
    active: bool = True
    notes: str = ""
    created_at: str = field(default_factory=utcnow)
    last_checked: str | None = None
    last_price: float | None = None
    last_in_stock: bool | None = None
    fail_count: int = 0


@dataclass
class Extraction:
    """One attempt at reading a price off a page."""

    price: float | None = None
    currency: str | None = None
    in_stock: bool | None = None
    title: str | None = None
    image: str | None = None
    method: str = "none"  # jsonld | microdata | meta | selector | heuristic | llm
    confidence: float = 0.0  # 0..1
    selector: str | None = None
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.price is not None


@dataclass
class Alert:
    kind: str  # target_hit | price_drop | price_rise | back_in_stock | out_of_stock | error
    product: str
    message: str
    price: float | None = None


@dataclass
class CheckResult:
    product: Product
    extraction: Extraction
    alerts: list = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.extraction.ok
