"""Reading price, stock and title off a product page - deterministically.

Strategies are tried best-first.  Each returns an Extraction with a confidence
score; the agent only pays for an LLM call when everything here comes up short.
"""

from __future__ import annotations

import json
import re
from typing import Any

from bs4 import BeautifulSoup, FeatureNotFound
from soupsieve import SelectorSyntaxError

from .models import Extraction
from .money import parse_price

OUT_OF_STOCK_MARKERS = [
    "out of stock",
    "sold out",
    "currently unavailable",
    "no longer available",
    "out-of-stock",
    "notify me when",
    "back in stock",
    "temporarily unavailable",
    "ausverkauft",
    "rupture de stock",
    "agotado",
    "esaurito",
]
IN_STOCK_MARKERS = [
    "in stock",
    "add to cart",
    "add to basket",
    "add to bag",
    "buy now",
    "in-stock",
]

# class/id fragments that suggest we found the *current* price
GOOD_HINTS = ["price", "prijs", "preis", "prezzo", "precio", "amount", "cost"]
STRONG_HINTS = [
    "sale",
    "now",
    "current",
    "our-price",
    "offer",
    "final",
    "deal",
    "special",
]
# Ancestor containers that mark the *main* product region of a page...
MAIN_REGION_HINTS = [
    "product_main",
    "product-main",
    "product-info",
    "product-detail",
    "product-page",
    "product-summary",
    "pdp",
    "buybox",
    "buy-box",
    "product-single",
    "productview",
    "product-form",
    "offers",
]
# ...and ones that mark recommendation carousels, upsells and listing cards.
ASIDE_REGION_HINTS = [
    "product_pod",
    "recommend",
    "related",
    "upsell",
    "cross-sell",
    "crosssell",
    "also-bought",
    "also-viewed",
    "similar",
    "carousel",
    "slider",
    "recently",
    "sidebar",
    "widget",
    "suggest",
    "you-may",
    "bundle",
    "accessor",
    "cart",
    "wishlist",
    "compare",
    "footer",
    "nav",
    "breadcrumb",
]

BAD_HINTS = [
    "old",
    "was",
    "list",
    "regular",
    "strike",
    "compare",
    "rrp",
    "msrp",
    "original",
    "shipping",
    "tax",
    "vat",
    "installment",
    "per-month",
    "monthly",
    "credit",
    "range",
    "from",
    "unit",
    "save",
    "discount",
    "total",
    "subtotal",
    "cart",
]


def attr_str(el: Any, name: str) -> str | None:
    """bs4 returns a list for multi-valued attributes; flatten to a string."""
    val = el.get(name)
    if val is None:
        return None
    if isinstance(val, (list, tuple)):
        return " ".join(str(v) for v in val)
    return str(val)


def _soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except FeatureNotFound:  # lxml not installed
        return BeautifulSoup(html, "html.parser")


def page_title(soup: BeautifulSoup) -> str | None:
    for sel, attr in (
        ('meta[property="og:title"]', "content"),
        ('meta[name="twitter:title"]', "content"),
    ):
        el = soup.select_one(sel)
        value = attr_str(el, attr) if el else None
        if value:
            return value.strip()[:200]
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(strip=True)[:200]
    if soup.title and soup.title.string:
        return soup.title.string.strip()[:200]
    return None


def detect_stock(soup: BeautifulSoup, html: str) -> bool | None:
    """True / False / None (unknown)."""
    for el in soup.select('[itemprop="availability"], link[itemprop="availability"]'):
        val = (
            attr_str(el, "href") or attr_str(el, "content") or el.get_text() or ""
        ).lower()
        if "instock" in val:
            return True
        if "outofstock" in val or "soldout" in val or "discontinued" in val:
            return False

    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True).lower())[:20000]
    buyable = any(
        m in text for m in ("add to cart", "add to basket", "add to bag", "buy now")
    )
    # Only trust prose about stock when there is no live buy button - plenty of
    # in-stock pages mention "notify me when..." for other variants.
    if not buyable:
        for marker in OUT_OF_STOCK_MARKERS:
            if marker != "back in stock" and marker in text:
                return False
    if buyable or any(m in text for m in IN_STOCK_MARKERS):
        return True
    return None


# --------------------------------------------------------------------------
# Strategy 1: an explicit CSS selector ("div.price" or "div.price@content")
# --------------------------------------------------------------------------
def from_selector(
    soup: BeautifulSoup,
    selector: str,
    method: str = "selector",
    confidence: float = 0.95,
) -> Extraction | None:
    if not selector:
        return None
    sel, _, attr = selector.partition("@")
    try:
        el = soup.select_one(sel.strip())
    except (SelectorSyntaxError, NotImplementedError, ValueError):
        return None  # a malformed or unsupported selector is not fatal
    if el is None:
        return None
    raw = attr_str(el, attr.strip()) if attr else el.get_text(" ", strip=True)
    price, currency = parse_price(raw or "")
    if price is None:
        return None
    return Extraction(
        price=price,
        currency=currency,
        method=method,
        confidence=confidence,
        selector=selector,
        note=f"matched {sel!r}",
    )


# --------------------------------------------------------------------------
# Strategy 2: schema.org JSON-LD - the most reliable signal when present
# --------------------------------------------------------------------------
def _walk(node, out: list[dict]) -> None:
    if isinstance(node, dict):
        out.append(node)
        for v in node.values():
            _walk(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk(v, out)


def from_jsonld(soup: BeautifulSoup) -> Extraction | None:
    nodes: list[dict] = []
    for tag in soup.find_all(
        "script", attrs={"type": re.compile("ld\\+json", re.IGNORECASE)}
    ):
        raw = tag.string or tag.get_text() or ""
        try:
            _walk(json.loads(raw), nodes)
        except (json.JSONDecodeError, TypeError):
            # Some sites emit several concatenated JSON objects or trailing commas.
            for chunk in re.findall(r"\{.*\}", raw, re.DOTALL):
                try:
                    _walk(json.loads(chunk), nodes)
                except (json.JSONDecodeError, TypeError):
                    continue

    best: Extraction | None = None
    name: str | None = None
    for node in nodes:
        types = node.get("@type") or node.get("type") or ""
        types = " ".join(types) if isinstance(types, list) else str(types)
        if "Product" in types and isinstance(node.get("name"), str):
            name = name or node["name"][:200]

        price_val = node.get("price", node.get("lowPrice", node.get("highPrice")))
        if (
            price_val is None
            and "Offer" not in types
            and "PriceSpecification" not in types
        ):
            continue
        if price_val is None:
            continue
        price, cur = parse_price(str(price_val))
        if price is None:
            continue
        currency = node.get("priceCurrency") or cur
        avail = str(node.get("availability", "")).lower()
        in_stock = (
            True
            if "instock" in avail
            else (False if avail and "outofstock" in avail else None)
        )
        cand = Extraction(
            price=price,
            currency=currency,
            in_stock=in_stock,
            method="jsonld",
            confidence=0.9,
            note="schema.org JSON-LD",
        )
        if best is None or (cand.price or 0) < (best.price or 0):
            best = cand  # multiple offers -> the buyable one is the lowest
    if best and name:
        best.title = name
    return best


# --------------------------------------------------------------------------
# Strategy 3: microdata / RDFa itemprops
# --------------------------------------------------------------------------
def from_microdata(soup: BeautifulSoup) -> Extraction | None:
    for sel in (
        '[itemprop="price"]',
        '[itemprop="lowPrice"]',
        '[property="product:price:amount"]',
        '[itemprop="priceSpecification"]',
    ):
        for el in soup.select(sel):
            raw = (
                attr_str(el, "content")
                or attr_str(el, "value")
                or el.get_text(" ", strip=True)
            )
            price, cur = parse_price(raw or "")
            if price is None:
                continue
            cur_el = soup.select_one('[itemprop="priceCurrency"]')
            currency = (attr_str(cur_el, "content") if cur_el else None) or cur
            return Extraction(
                price=price,
                currency=currency,
                method="microdata",
                confidence=0.8,
                selector=sel,
                note="itemprop",
            )
    return None


# --------------------------------------------------------------------------
# Strategy 4: meta tags (OpenGraph and friends)
# --------------------------------------------------------------------------
META_PRICE = [
    ('meta[property="product:price:amount"]', "content"),
    ('meta[property="og:price:amount"]', "content"),
    ('meta[name="twitter:data1"]', "content"),
    ('meta[itemprop="price"]', "content"),
]
META_CURRENCY = [
    ('meta[property="product:price:currency"]', "content"),
    ('meta[property="og:price:currency"]', "content"),
]


def from_meta(soup: BeautifulSoup) -> Extraction | None:
    currency = None
    for sel, attr in META_CURRENCY:
        el = soup.select_one(sel)
        value = attr_str(el, attr) if el else None
        if value:
            currency = value.strip().upper()
            break
    for sel, attr in META_PRICE:
        el = soup.select_one(sel)
        value = attr_str(el, attr) if el else None
        if not value:
            continue
        price, cur = parse_price(value)
        if price is not None:
            return Extraction(
                price=price,
                currency=currency or cur,
                method="meta",
                confidence=0.75,
                selector=f"{sel}@{attr}",
                note="meta tag",
            )
    return None


# --------------------------------------------------------------------------
# Strategy 5: class/id heuristics over visible text
# --------------------------------------------------------------------------
def _css_path(el) -> str:
    """A short, reusable selector for an element."""
    ident = attr_str(el, "id")
    if ident and re.fullmatch(r"[A-Za-z][\w-]*", ident):
        return f"#{ident}"
    for attr in ("data-testid", "data-test", "data-qa", "itemprop"):
        value = attr_str(el, attr)
        if value:
            return f'{el.name}[{attr}="{value}"]'
    classes = [c for c in (el.get("class") or []) if re.fullmatch(r"[A-Za-z][\w-]*", c)]
    if classes:
        return el.name + "".join(f".{c}" for c in classes[:3])
    return el.name


def _region_score(el) -> float:
    """Reward prices in the main product region, punish carousel/listing cards."""
    delta = 0.0
    node, depth = el.parent, 0
    while node is not None and depth < 7 and getattr(node, "name", None):
        if node.name in ("aside", "nav", "footer", "header"):
            delta -= 0.30
        if node.name == "li":
            delta -= 0.15  # a price inside a list item is usually a card
        ident = " ".join(
            [
                attr_str(node, "class") or "",
                attr_str(node, "id") or "",
                attr_str(node, "itemtype") or "",
            ]
        ).lower()
        if any(h in ident for h in ASIDE_REGION_HINTS):
            delta -= 0.35
        if any(h in ident for h in MAIN_REGION_HINTS):
            delta += 0.20
        node, depth = node.parent, depth + 1
    return max(-0.5, min(delta, 0.25))


def from_heuristics(soup: BeautifulSoup) -> list[Extraction]:
    out: list[Extraction] = []
    order = 0
    for el in soup.find_all(
        [
            "span",
            "div",
            "p",
            "b",
            "strong",
            "ins",
            "bdi",
            "h1",
            "h2",
            "h3",
            "td",
            "meta",
        ]
    ):
        attrs = " ".join(
            [
                attr_str(el, "class") or "",
                attr_str(el, "id") or "",
                attr_str(el, "data-testid") or "",
                attr_str(el, "itemprop") or "",
            ]
        ).lower()
        if not any(h in attrs for h in GOOD_HINTS):
            continue

        text = (
            attr_str(el, "content")
            if el.name == "meta"
            else el.get_text(" ", strip=True)
        )
        if not text or len(text) > 40:
            continue
        price, currency = parse_price(text)
        if price is None or price <= 0:
            continue

        order += 1
        score = 0.45 + _region_score(el)
        if any(h in attrs for h in STRONG_HINTS):
            score += 0.15
        if any(h in attrs for h in BAD_HINTS):
            score -= 0.30
        if any(p.name in ("del", "s", "strike") for p in el.parents if p.name):
            score -= 0.35
        if currency or re.search(r"[$€£¥₹]", text):
            score += 0.10
        if el.name == "meta":
            score += 0.05
        # Tiny document-order nudge: the real price almost always appears
        # before the recommendations.
        score -= min(order, 50) * 0.001
        out.append(
            Extraction(
                price=price,
                currency=currency,
                method="heuristic",
                confidence=max(0.05, min(score, 0.72)),
                selector=_css_path(el),
                note=f"text {text!r}",
            )
        )

    out.sort(key=lambda e: (-e.confidence, e.price or 0))
    return out[:8]


# --------------------------------------------------------------------------
def extract(
    html: str, selector: str | None = None, learned_selector: str | None = None
) -> tuple[Extraction, list[Extraction]]:
    """Run every deterministic strategy; return (best, all candidates)."""
    soup = _soup(html)
    candidates: list[Extraction] = []

    for sel, method, conf in (
        (selector, "selector", 0.95),
        (learned_selector, "learned-selector", 0.85),
    ):
        if sel:
            got = from_selector(soup, sel, method=method, confidence=conf)
            if got:
                candidates.append(got)

    for fn in (from_jsonld, from_microdata, from_meta):
        got = fn(soup)
        if got:
            candidates.append(got)
    candidates.extend(from_heuristics(soup))

    title = page_title(soup)
    stock = detect_stock(soup, html)
    for c in candidates:
        c.title = c.title or title
        if c.in_stock is None:
            c.in_stock = stock

    if not candidates:
        return Extraction(
            title=title, in_stock=stock, method="none", note="no price found"
        ), []

    candidates.sort(key=lambda e: -e.confidence)
    return candidates[0], candidates
