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

from . import sites
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
# Strategy: a known retailer's own markup
# --------------------------------------------------------------------------
def from_site_rule(soup: BeautifulSoup, url: str) -> Extraction | None:
    rule = sites.match(url)
    if rule is None:
        return None
    for sel in rule.price:
        got = from_selector(soup, sel, method=f"site:{rule.name}", confidence=0.92)
        if got:
            got.currency = got.currency or rule.currency
            if rule.title:
                for tsel in rule.title:
                    el = soup.select_one(tsel)
                    if el and el.get_text(strip=True):
                        got.title = el.get_text(" ", strip=True)[:200]
                        break
            if any(soup.select_one(s) is not None for s in rule.out_of_stock):
                got.in_stock = False
            elif any(soup.select_one(s) is not None for s in rule.in_stock):
                got.in_stock = True
            got.note = f"{rule.name} selector"
            return got
    return None


# --------------------------------------------------------------------------
# Strategy: product JSON embedded in the page (Next.js, Nuxt, Redux dumps)
# --------------------------------------------------------------------------
PRICE_KEYS = {
    "currentprice": 1.0,
    "saleprice": 0.95,
    "finalprice": 0.95,
    "priceamount": 0.9,
    "price": 0.8,
    "listprice": 0.5,
    "wasprice": 0.3,
    "regularprice": 0.4,
    "minprice": 0.6,
    "displayprice": 0.85,
    "pricevalue": 0.85,
}
CURRENCY_KEYS = ("currency", "currencyunit", "currencycode", "pricecurrency")
GOOD_PATH = ("priceinfo", "currentprice", "offers", "product", "pricing", "buybox")
BAD_PATH = (
    "shipping",
    "recommend",
    "similar",
    "related",
    "carousel",
    "review",
    "variant",
    "installment",
    "warranty",
    "protection",
    "addon",
    "bundle",
    "subscription",
    "tax",
    "fee",
    "seller",
)
_JSON_SCRIPT_RE = re.compile(
    r"(?:window\.)?__(?:NEXT_DATA__|PRELOADED_STATE__|INITIAL_STATE__|"
    r"NUXT__|APOLLO_STATE__)\s*=\s*(\{.*?\})\s*(?:;|</script>)",
    re.DOTALL,
)


def _json_blobs(soup: BeautifulSoup, html: str) -> list[dict]:
    blobs: list[dict] = []
    for tag in soup.find_all("script"):
        stype = (attr_str(tag, "type") or "").lower()
        sid = (attr_str(tag, "id") or "").lower()
        raw = tag.string or tag.get_text() or ""
        if not raw.strip():
            continue
        if "json" in stype or sid in ("__next_data__", "__nuxt_data__"):
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(parsed, dict):
                blobs.append(parsed)
    for m in _JSON_SCRIPT_RE.finditer(html):
        try:
            parsed = json.loads(m.group(1))
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            blobs.append(parsed)
    return blobs


def _scan_json(
    node: Any,
    path: str,
    out: list[tuple[float, float, str | None, str]],
    depth: int = 0,
) -> None:
    if depth > 12 or len(out) > 400:
        return
    if isinstance(node, dict):
        currency = None
        for ck in CURRENCY_KEYS:
            for key, val in node.items():
                if key.lower() == ck and isinstance(val, str) and 2 <= len(val) <= 4:
                    currency = val.upper()
                    break
            if currency:
                break
        for key, val in node.items():
            lkey = key.lower().replace("_", "")
            weight = PRICE_KEYS.get(lkey)
            if weight and isinstance(val, (int, float, str)):
                price, cur = parse_price(str(val))
                if price is not None and 0 < price < 10_000_000:
                    lpath = (path + "." + key).lower()
                    score = 0.55 * weight
                    score += 0.12 if any(g in lpath for g in GOOD_PATH) else 0.0
                    score -= 0.45 if any(b in lpath for b in BAD_PATH) else 0.0
                    score -= min(depth, 10) * 0.012
                    out.append((score, price, currency or cur, path + "." + key))
            _scan_json(val, f"{path}.{key}", out, depth + 1)
    elif isinstance(node, list):
        for i, val in enumerate(node[:40]):
            _scan_json(val, f"{path}[{i}]", out, depth + 1)


def from_embedded_json(soup: BeautifulSoup, html: str) -> Extraction | None:
    """Read the price out of a page's own JSON payload.

    Modern storefronts ship the product as JSON and render it client-side; the
    number is in the HTML, just not in a tag we can select.
    """
    candidates: list[tuple[float, float, str | None, str]] = []
    for blob in _json_blobs(soup, html)[:6]:
        _scan_json(blob, "$", candidates)
    if not candidates:
        return None
    candidates.sort(key=lambda c: -c[0])
    score, price, currency, path = candidates[0]
    if score < 0.25:
        return None
    return Extraction(
        price=price,
        currency=currency,
        method="embedded-json",
        confidence=min(0.70, max(0.35, score)),
        note=f"embedded JSON at {path[:80]}",
    )


# --------------------------------------------------------------------------
def extract(
    html: str,
    selector: str | None = None,
    learned_selector: str | None = None,
    url: str | None = None,
) -> tuple[Extraction, list[Extraction]]:
    """Run every deterministic strategy; return (best, all candidates)."""
    soup = _soup(html)
    candidates: list[Extraction] = []

    if url:
        site_hit = from_site_rule(soup, url)
        if site_hit:
            candidates.append(site_hit)

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
    embedded = from_embedded_json(soup, html)
    if embedded:
        candidates.append(embedded)
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
