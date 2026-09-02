"""Find a product page from a product *name*.

You rarely have a URL to hand - you have "sony wh-1000xm5". This module turns a
name into candidate product pages, prices each one with the normal extraction
cascade, and (optionally) asks Claude which candidate actually matches what you
asked for.

Search goes through DuckDuckGo's HTML endpoint: no API key, no account, and it
returns real retailer links rather than a JS shell.
"""

from __future__ import annotations

import base64
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import requests

from . import sites
from .extract import attr_str, extract
from .fetcher import FetchError, _throttle, fetch
from .money import format_price

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
    """Unwrap the redirect URLs search engines wrap their results in."""
    if "uddg=" in href:  # DuckDuckGo
        return unquote(parse_qs(urlparse(href).query).get("uddg", [""])[0])
    if "bing.com/ck/a" in href:  # Bing: base64 in the u= parameter
        raw = parse_qs(urlparse(href).query).get("u", [""])[0]
        if raw.startswith("a1"):
            payload = raw[2:]
            try:
                return base64.urlsafe_b64decode(
                    payload + "=" * (-len(payload) % 4)
                ).decode("utf-8", "ignore")
            except (ValueError, UnicodeDecodeError):
                return ""
    return href


def _looks_like_product(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if not host or any(bad in host for bad in NON_SHOP):
        return False
    if EXCLUDE_PATH.search(parsed.path):
        return False
    return bool(_PRODUCT_RE.search(parsed.path))


# Three engines, tried in order. One of them rate-limiting should not take the
# whole feature down with it, and none of them is hit hard: results are found
# on the first engine almost every time.
ENGINES: tuple[tuple[str, str], ...] = (
    ("duckduckgo", "https://html.duckduckgo.com/html/?q={q}"),
    ("mojeek", "https://www.mojeek.com/search?q={q}"),
    ("bing", "https://www.bing.com/search?q={q}"),
)

# Where to look when the open web only offers category pages, blog posts and
# reviews - which is what a query like "ikea billy bookcase" returns.
FALLBACK_SITES = "site:amazon.com OR site:walmart.com OR site:ebay.com"


class SearchBlocked(RuntimeError):
    """Every engine refused to answer - usually a rate limit."""


def _parse_results(engine: str, html: str) -> list[tuple[str, str]]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    out: list[tuple[str, str]] = []

    if engine == "duckduckgo":
        anchors = soup.select("a.result__a")
    elif engine == "mojeek":
        anchors = soup.select("a.title, ul.results-standard li h2 a")
    else:
        anchors = soup.select("li.b_algo h2 a")

    for anchor in anchors:
        href = _decode(attr_str(anchor, "href") or "")
        if href.startswith("http"):
            out.append((href, anchor.get_text(" ", strip=True)[:160]))
    return out


def _ask_engine(
    engine: str, url_template: str, terms: str, cfg: dict
) -> list[tuple[str, str]]:
    """Query one engine. Returns [] when it declines to answer."""
    _throttle(url_template, float(cfg.get("min_delay_per_domain", 3.0)))
    try:
        resp = requests.get(
            url_template.format(q=quote_plus(terms)),
            headers={
                "User-Agent": cfg["user_agent"],
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=cfg.get("timeout", 25),
        )
    except requests.RequestException:
        return []
    # DuckDuckGo answers a rate limit with 202 and an "anomaly" page rather
    # than an error status, so status alone is not enough to go on.
    if resp.status_code != 200 or "anomaly" in resp.text[:4000].lower():
        return []
    return _parse_results(engine, resp.text)


def find_urls(
    query: str, cfg: dict, retailers: list[str] | None = None, limit: int = 12
) -> list[tuple[str, str]]:
    """Search for candidate product pages. Returns [(url, title)].

    Stops at the first engine and phrasing that yields real product pages, so
    the common case is a single request.
    """
    domains: list[str] = []
    if retailers:
        for name in retailers:
            rule = next(
                (r for r in sites.RULES if r.name.lower().startswith(name.lower())),
                None,
            )
            domains.extend(rule.domains[:1] if rule else [name])

    if domains:
        variants = [f"{query} " + " OR ".join(f"site:{d}" for d in domains)]
    else:
        variants = [query, f"{query} buy price", f"{query} {FALLBACK_SITES}"]

    answered = False
    for terms in variants:
        for engine, template in ENGINES:
            raw = _ask_engine(engine, template, terms, cfg)
            if not raw:
                continue
            answered = True
            found = _keep_products(raw, limit)
            if found:
                return found

    if not answered:
        raise SearchBlocked(
            "every search engine declined to answer (usually a rate limit). "
            "Wait a minute, or paste the product link instead."
        )
    return []


def _keep_products(raw: list[tuple[str, str]], limit: int) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for href, title in raw:
        if not _looks_like_product(href):
            continue
        canonical = sites.canonical_url(href)
        if canonical in seen:
            continue
        seen.add(canonical)
        found.append((canonical, title))
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
