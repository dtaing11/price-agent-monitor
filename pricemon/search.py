"""Find a product page from a product *name*.

You rarely have a URL to hand - you have "sony wh-1000xm5". This module turns a
name into candidate product pages, prices each one with the normal extraction
cascade, and (optionally) asks Claude which candidate actually matches what you
asked for.

Search goes through DuckDuckGo's HTML endpoint: no API key, no account, and it
returns real retailer links rather than a JS shell.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import requests

from . import sites
from .extract import attr_str, extract
from .fetcher import FetchError, fetch
from .money import format_price

SEARCH_URL = "https://html.duckduckgo.com/html/?q={query}"

# Path shapes that mean "this is one product", per retailer and in general.
PRODUCT_PATTERNS = (
    r"/dp/[A-Z0-9]{10}",  # Amazon
    r"/gp/product/[A-Z0-9]{10}",  # Amazon (older)
    r"/ip/[^/]+/\d+",  # Walmart
    r"/itm/\d+",  # eBay
    r"/p/[^/]+/-/A-\d+",  # Target
    r"/site/[^/]+/\d+\.p",  # Best Buy
    r"/listing/\d+",  # Etsy
    r"/p/[^/]{3,}",  # IKEA, Zalando, many others
    r"/product/[^/]{3,}",
    r"/products/[^/]{3,}",
    r"/pd/[^/]{3,}",
    r"/app/\d+",  # Steam
    r"/-p-\d+",
)
_PRODUCT_RE = re.compile("|".join(PRODUCT_PATTERNS), re.IGNORECASE)

# Places that are never a buyable product page.
NON_SHOP = (
    "wikipedia.org",
    "reddit.com",
    "youtube.com",
    "youtu.be",
    "facebook.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "tiktok.com",
    "pinterest.com",
    "quora.com",
    "medium.com",
    "rtings.com",
    "cnet.com",
    "techradar.com",
    "tomsguide.com",
    "theverge.com",
    "wired.com",
    "consumerreports.org",
    "trustpilot.com",
    "camelcamelcamel.com",
    "slickdeals.net",
    "pcpartpicker.com",
    "news.google.com",
    "linkedin.com",
    "answers.microsoft.com",
)
EXCLUDE_PATH = re.compile(
    r"/(?:search|s\b|sch/|browse|category|categories|c/|b/|deals|blog|help|"
    r"support|reviews?/|compare|store-locator)",
    re.IGNORECASE,
)


@dataclass
class SearchResult:
    title: str
    url: str
    retailer: str
    price: float | None = None
    currency: str | None = None
    in_stock: bool | None = None
    method: str = ""
    note: str = ""

    def describe(self) -> str:
        stock = (
            ""
            if self.in_stock is None
            else (" · in stock" if self.in_stock else " · OUT OF STOCK")
        )
        price = (
            format_price(self.price, self.currency) if self.price else "price not read"
        )
        return f"{price:>12}  {self.retailer:<16} {self.title[:58]}{stock}"


def _decode(href: str) -> str:
    """DuckDuckGo wraps results in a redirect; unwrap it."""
    if "uddg=" in href:
        target = parse_qs(urlparse(href).query).get("uddg", [""])[0]
        return unquote(target)
    return href


def _looks_like_product(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if not host or any(bad in host for bad in NON_SHOP):
        return False
    if EXCLUDE_PATH.search(parsed.path):
        return False
    return bool(_PRODUCT_RE.search(parsed.path))


def find_urls(
    query: str, cfg: dict, retailers: list[str] | None = None, limit: int = 12
) -> list[tuple[str, str]]:
    """Search the web for candidate product pages. Returns [(url, title)]."""
    terms = query.strip()
    if retailers:
        domains: list[str] = []
        for name in retailers:
            rule = next(
                (r for r in sites.RULES if r.name.lower().startswith(name.lower())),
                None,
            )
            domains.extend(rule.domains[:1] if rule else [name])
        terms += " " + " OR ".join(f"site:{d}" for d in domains)

    from bs4 import BeautifulSoup

    resp = requests.get(
        SEARCH_URL.format(query=quote_plus(terms)),
        headers={
            "User-Agent": cfg["user_agent"],
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        },
        timeout=cfg.get("timeout", 25),
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for anchor in soup.select("a.result__a, a.result__url"):
        href = _decode(attr_str(anchor, "href") or "")
        if not href.startswith("http") or not _looks_like_product(href):
            continue
        canonical = sites.canonical_url(href)
        if canonical in seen:
            continue
        seen.add(canonical)
        found.append((canonical, anchor.get_text(" ", strip=True)[:160]))
        if len(found) >= limit:
            break
    return found


def _retailer_of(url: str) -> str:
    rule = sites.match(url)
    return rule.name if rule else urlparse(url).netloc.replace("www.", "")


def _price_one(url: str, title: str, cfg: dict) -> SearchResult:
    retailer = _retailer_of(url)
    try:
        html, final = fetch(url, cfg)
    except FetchError as exc:
        return SearchResult(title=title, url=url, retailer=retailer, note=str(exc)[:80])
    blocked = sites.looks_blocked(html)
    if blocked:
        return SearchResult(title=title, url=url, retailer=retailer, note=blocked)

    best, _ = extract(html, url=final)
    return SearchResult(
        title=best.title or title,
        url=sites.canonical_url(final),
        retailer=retailer,
        price=best.price,
        currency=best.currency,
        in_stock=best.in_stock,
        method=best.method,
        note="" if best.price else "no price found on the page",
    )


def search(
    query: str,
    cfg: dict,
    retailers: list[str] | None = None,
    limit: int = 6,
    price_them: bool = True,
) -> list[SearchResult]:
    """Name in, priced product pages out - best match first."""
    candidates = find_urls(query, cfg["fetch"], retailers=retailers, limit=limit * 2)
    if not candidates:
        return []
    candidates = candidates[: max(limit, 1)]

    if not price_them:
        return [
            SearchResult(title=t, url=u, retailer=_retailer_of(u))
            for u, t in candidates
        ]

    # Different hosts, so these can overlap; same-host requests still serialise
    # on the fetcher's per-domain throttle.
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(lambda c: _price_one(c[0], c[1], cfg["fetch"]), candidates)
        )

    # The same shop can surface twice under slightly different URLs.
    deduped: list[SearchResult] = []
    seen_keys: set[tuple[str, str]] = set()
    for r in results:
        key = (r.retailer, r.title[:60].lower())
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(r)
    results = deduped

    scored = sorted(
        results,
        key=lambda r: (
            r.price is None,  # priced results first
            not _matches(query, r.title),  # then ones matching the words
            r.in_stock is False,  # in-stock before out-of-stock
            r.price or 0,  # then cheapest
        ),
    )
    return scored


def _matches(query: str, title: str) -> bool:
    """Do the meaningful words of the query appear in the title?"""
    words = [w for w in re.split(r"[^a-z0-9]+", query.lower()) if len(w) > 2]
    if not words:
        return True
    title_l = title.lower()
    hits = sum(1 for w in words if w in title_l)
    return hits >= max(1, len(words) // 2)


def rank_with_ai(query: str, results: list[SearchResult], llm) -> list[SearchResult]:
    """Ask Claude which candidates really are the product asked for.

    Search engines happily return accessories, older models and bundles; a
    quick read of the titles removes most of that.
    """
    if not results or not getattr(llm, "available", False):
        return results
    listing = "\n".join(
        f"{i}. {r.title[:110]} | {r.retailer} | "
        f"{format_price(r.price, r.currency) if r.price else 'no price'}"
        for i, r in enumerate(results)
    )
    prompt = (
        f"A shopper asked to track this product: {query!r}\n\n"
        f"Search returned these pages:\n{listing}\n\n"
        "Which entries are that exact product (not an accessory, case, cable, "
        "older or newer model, multi-pack or unrelated item)?\n"
        'Reply with ONLY JSON: {"order": [indices best match first], '
        '"reject": [indices that are the wrong product]}'
    )
    try:
        raw = (
            llm._ask_cli(prompt)
            if llm.backend == "claude_cli"
            else llm._ask_api(prompt)
        )
        from .llm import _extract_json

        data = _extract_json(raw) or {}
    except Exception:  # noqa: BLE001 - ranking is a nicety, never a blocker
        return results

    reject = {
        int(i)
        for i in data.get("reject", [])
        if isinstance(i, (int, str)) and str(i).isdigit()
    }
    order = [int(i) for i in data.get("order", []) if str(i).isdigit()]
    ranked = [results[i] for i in order if 0 <= i < len(results) and i not in reject]
    ranked += [r for i, r in enumerate(results) if i not in reject and r not in ranked]
    return ranked or results
