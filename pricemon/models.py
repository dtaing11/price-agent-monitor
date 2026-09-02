"""Core data types shared across the agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


def utcnow() -> str:
    """ISO-8601 UTC timestamp, second resolution."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Product:
    """A page we watch."""

    name: str
    url: str
    id: Optional[int] = None
    selector: Optional[str] = None           # user-pinned, always tried first
    learned_selector: Optional[str] = None   # discovered by the LLM, self-healing
    target_price: Optional[float] = None
    currency: Optional[str] = None
    active: bool = True
    notes: str = ""
    created_at: str = field(default_factory=utcnow)
    last_checked: Optional[str] = None
    last_price: Optional[float] = None
    last_in_stock: Optional[bool] = None
    fail_count: int = 0


@dataclass
class Extraction:
    """One attempt at reading a price off a page."""

    price: Optional[float] = None
    currency: Optional[str] = None
    in_stock: Optional[bool] = None
    title: Optional[str] = None
    method: str = "none"        # jsonld | microdata | meta | selector | heuristic | llm
    confidence: float = 0.0     # 0..1
    selector: Optional[str] = None
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.price is not None


@dataclass
class Alert:
    kind: str                   # target_hit | price_drop | price_rise | back_in_stock | out_of_stock | error
    product: str
    message: str
    price: Optional[float] = None


@dataclass
class CheckResult:
    product: Product
    extraction: Extraction
    alerts: list = field(default_factory=list)
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.extraction.ok
