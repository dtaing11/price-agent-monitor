"""Find a product page from a product *name*.

You rarely have a URL to hand - you have "sony wh-1000xm5". This module turns a
name into candidate product pages, prices each one with the normal extraction
cascade, and (optionally) asks Claude which candidate actually matches what you
asked for.

Search goes through DuckDuckGo's HTML endpoint: no API key, no account, and it
returns real retailer links rather than a JS shell.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

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
    matched: str | None = None  # the specific product this result was found for

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


# Words that carry no search signal in a product title.
FILLER = {
    "with",
    "and",
    "for",
    "the",
    "a",
    "an",
    "featuring",
    "includes",
    "included",
    "new",
    "genuine",
    "official",
    "original",
    "brand",
    "authentic",
    "premium",
    "professional",
    "ultimate",
    "advanced",
    "series",
    "edition",
    "version",
    "model",
    "inch",
    "in",
    "of",
    "by",
    "plus",
}

# Where a marketing description usually starts.
SEPARATORS = re.compile(r"\s+[-–—|·:]\s+|[,;(\[]|\s{2,}")

# "WH-1000XM5", "3S", "M2", "XM4" - a token mixing letters and digits is almost
# always the model, and the model is what actually finds the product.
MODEL_TOKEN = re.compile(r"^(?=.*\d)(?=.*[A-Za-z])[A-Za-z0-9][A-Za-z0-9\-/.]*$")


def normalize_query(text: str, max_words: int = 8) -> str:
    """Turn a pasted product title into something a search engine can use.

    Shop titles are marketing sentences - "Logitech MX Master 3S - Wireless
    Performance Mouse with Ultra-fast Scrolling, Ergo, 8K DPI, Track on
    Glass..." - and searching one verbatim returns the brand's home page, not
    the product. Brand plus model is what finds it.
    """
    text = " ".join(str(text or "").split())
    if not text:
        return ""

    # 1. Cut at the first separator, as long as something useful is left.
    head = SEPARATORS.split(text)[0].strip()
    if len(head.split()) >= 2:
        text = head

    words = [w for w in text.split() if w.strip()]

    # 2. Stop right after the model number, if one shows up early.
    for i, word in enumerate(words[:6]):
        cleaned = word.strip("\"'.,")
        if i > 0 and MODEL_TOKEN.match(cleaned) and any(c.isdigit() for c in cleaned):
            words = words[: i + 1]
            break

    # 3. Drop filler, keeping anything that looks like a model.
    kept = [
        w for w in words if w.lower().strip(".,") not in FILLER or MODEL_TOKEN.match(w)
    ]
    kept = kept or words

    # 4. Keep it short; long queries are what caused the problem.
    out = " ".join(kept[:max_words])
    return out.strip(" -–—|,;:\"'") or text


PLANNER_PROMPT = """A shopper wants to track the price of something and typed:

  {query}

Decide what they are actually shopping for.

If that names one specific product (a brand and model), just return it.

If it names a *category* - "thunderbolt dock", "good office chair" - name the
specific products a knowledgeable person would actually shop for. Rules:
- real models currently sold, with brand and model number, e.g.
  "CalDigit TS4 Thunderbolt 4 Dock", not "a Thunderbolt dock"
- the ones most people would genuinely consider, best-regarded first
- spread across price points where that makes sense
- no accessories, no discontinued models, no made-up names

Reply with ONLY JSON:
{{"specific": true|false,
  "reading": "one short line on what you think they want",
  "products": ["Brand Model", ...]}}"""


def looks_vague(query: str) -> bool:
    """Might this be a category rather than one product?

    Deliberately structural, not a list of nouns: any list of product words is
    a list of things the agent cannot handle, and "outdoor strip light" is
    exactly what falls off the end of one. A model number - a token mixing
    letters and digits - means the shopper already knows what they want.
    Anything short without one might be a category, and the planner decides.

    This is only a first guess, and a cheap one. When it is wrong the search
    itself corrects it: a query that finds no product pages gets planned
    anyway.
    """
    words = [w for w in re.split(r"[^A-Za-z0-9-]+", query.strip().lower()) if w]
    if not words:
        return True
    if any(MODEL_TOKEN.match(w) and any(c.isdigit() for c in w) for w in words):
        return False
    return len(words) <= 5


def plan_products(query: str, llm, limit: int = 4) -> tuple[list[str], str]:
    """Turn a vague ask into specific products worth pricing.

    This is the step that makes a category searchable at all: "thunderbolt
    dock" has no product page, but "CalDigit TS4" does. Suggestions are only a
    plan - each one is then searched and priced for real, so anything invented
    or discontinued simply finds nothing and drops out.
    """
    if not getattr(llm, "available", False):
        return [], ""
    try:
        raw = (
            llm._ask_cli(PLANNER_PROMPT.format(query=query))
            if llm.backend == "claude_cli"
            else llm._ask_api(PLANNER_PROMPT.format(query=query))
        )
        from .llm import _extract_json

        data = _extract_json(raw) or {}
    except Exception as exc:  # noqa: BLE001 - never block a search on planning
        # Swallowing this silently made a working planner look broken, so the
        # reason travels back with the empty plan.
        logger.info("could not plan %r: %s", query, exc)
        return [], f"(could not work out what to look for: {type(exc).__name__})"

    if data.get("specific"):
        return [], str(data.get("reading") or "")
    products = [
        str(p).strip()
        for p in (data.get("products") or [])
        if isinstance(p, str) and 2 < len(str(p).strip()) < 90
    ]
    return products[:limit], str(data.get("reading") or "")


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


logger = logging.getLogger(__name__)


# When a query finds only category pages and listicles, narrowing to the big
# retailers is what turns it into product pages.
FALLBACK_SITES = "site:amazon.com OR site:walmart.com OR site:ebay.com"


class SearchBlocked(RuntimeError):
    """Every search provider is out of budget or unreachable."""


def _source_engines(
    query: str, cfg: dict, domains: list[str], limit: int
) -> tuple[list[tuple[str, str]], bool]:
    """Web search, through the rate-limit-proof layer.

    Everything about quota - counting calls before they go out, choosing the
    provider with the most budget left, backing off, caching, coalescing
    duplicate questions - lives in `pricemon.searchlayer`. This function only
    turns a product query into search terms and keeps the results that look
    like something you can buy.
    """
    from .searchlayer import AllProvidersExhausted
    from .searchlayer import SearchResult as LayerResult

    trimmed = normalize_query(query)
    short = " ".join(trimmed.split()[:3])
    phrasings = [trimmed]
    if short and short != trimmed:
        phrasings.append(short)
    if query.strip() != trimmed:
        phrasings.append(query.strip())

    if domains:
        sites_filter = " " + " OR ".join(f"site:{d}" for d in domains)
        variants = [f"{p}{sites_filter}" for p in phrasings]
    else:
        variants = [*phrasings, f"{trimmed} buy price", f"{trimmed} {FALLBACK_SITES}"]

    router = _router()
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    answered = False
    enough = min(limit, 4)

    for terms in variants:
        try:
            hits: list[LayerResult] = _run_async(router.search(terms, count=limit * 2))
        except AllProvidersExhausted as exc:
            logger.info("search providers exhausted for %r: %s", terms, exc)
            break
        except Exception as exc:  # noqa: BLE001 - a bad query must not kill a check
            logger.info("search failed for %r: %s", terms, exc)
            continue

        answered = True
        for hit in hits:
            if not _looks_like_product(hit.url):
                continue
            canonical = sites.canonical_url(hit.url)
            if canonical not in seen:
                seen.add(canonical)
                found.append((canonical, hit.title))
        if len(found) >= enough:
            break
    return found[:limit], answered


_ROUTER = None


def _router():
    """One router per process, so its cache and ledger are shared."""
    global _ROUTER
    if _ROUTER is None:
        from . import config as config_mod
        from .searchlayer import build_router

        _ROUTER = build_router(config_mod.load().get("search"))
    return _ROUTER


def reset_router() -> None:
    """Forget the router, so changed settings take effect without a restart."""
    global _ROUTER
    _ROUTER = None


def _run_async(coro):
    """Bridge to the async layer from this synchronous codebase."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # Already inside a loop (the app's worker threads): give it its own.
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


WEB_SEARCH_PROMPT = """Find product pages where "{query}" can be bought right now.

Use web search. Return direct product pages at shops - Amazon /dp/, Walmart
/ip/, Best Buy, Target, Home Depot, the brand's own store, wherever it is
actually sold. Not category pages, not reviews, not listicles, not "best of"
articles.

Prefer the exact product asked for. Reply with ONLY JSON:
{{"urls": ["https://...", ...]}}"""

# Shops whose own search page answers a plain HTTP request with real product
# links. Most large retailers either block this outright or render results in
# JavaScript, so this list is short by measurement, not by choice.
RETAILER_SEARCH = ("https://www.newegg.com/p/pl?d={q}",)


def _source_claude_web(query: str, llm, limit: int) -> list[tuple[str, str]]:
    """Claude, with web search, reading results the way a person would.

    This is what rescues a query the free engines cannot serve - either
    because they are rate-limiting, or because they answer a product name with
    the brand's home page, which is not something you can buy. It runs through
    the same login as the rest of the agent.
    """
    if llm is None or not getattr(llm, "available", False):
        return []
    try:
        raw = llm.ask_with_web_search(WEB_SEARCH_PROMPT.format(query=query))
    except Exception as exc:  # noqa: BLE001 - one source failing is not fatal
        logger.info("web search via Claude failed for %r: %s", query, exc)
        return []

    from .llm import _extract_json

    data = _extract_json(raw) or {}
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for url in data.get("urls") or []:
        if not isinstance(url, str) or not url.startswith("http"):
            continue
        if not _looks_like_product(url):
            continue
        canonical = sites.canonical_url(url)
        if canonical not in seen:
            seen.add(canonical)
            out.append((canonical, ""))
    return out[:limit]


def _source_retailer_search(query: str, cfg: dict, limit: int) -> list[tuple[str, str]]:
    """Ask shops' own search pages, which only ever return product links."""
    from bs4 import BeautifulSoup

    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for template in RETAILER_SEARCH:
        url = template.format(q=quote_plus(normalize_query(query)))
        _throttle(url, float(cfg.get("min_delay_per_domain", 3.0)))
        try:
            resp = requests.get(
                url,
                headers={
                    "User-Agent": cfg["user_agent"],
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                timeout=cfg.get("timeout", 25),
            )
        except requests.RequestException:
            continue
        if resp.status_code != 200:
            continue
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup.find_all("a", href=True):
            href = urljoin(resp.url, attr_str(tag, "href") or "")
            if not _looks_like_product(href):
                continue
            canonical = sites.canonical_url(href)
            if canonical in seen:
                continue
            seen.add(canonical)
            found.append((canonical, tag.get_text(" ", strip=True)[:160]))
            if len(found) >= limit:
                return found
    return found


def find_urls(
    query: str,
    cfg: dict,
    retailers: list[str] | None = None,
    limit: int = 12,
    llm=None,
    budget: dict | None = None,
) -> list[tuple[str, str]]:
    """Find product pages for a query, from whichever source can.

    Sources are tried cheapest first and stop as soon as there is enough to
    work with, so the ordinary case still costs one HTTP request:

      1. search engines        free and fast, but they rate-limit, and they
                               answer a product name with a brand home page
      2. Claude + web search   slower, uses your login, and reads results the
                               way a person would
      3. a shop's own search   only ever returns product pages, for the shops
                               that answer a plain request

    `budget` caps how often the slow source may run within one search, so
    planning four products cannot turn into four web searches.
    """
    domains: list[str] = []
    if retailers:
        for name in retailers:
            rule = next(
                (r for r in sites.RULES if r.name.lower().startswith(name.lower())),
                None,
            )
            domains.extend(rule.domains[:1] if rule else [name])

    found, answered = _source_engines(query, cfg, domains, limit)
    if len(found) >= min(limit, 2):
        return found

    if budget is None or budget.get("claude", 0) > 0:
        if budget is not None:
            budget["claude"] = budget.get("claude", 0) - 1
        already = {u for u, _ in found}
        found += [
            (u, t) for u, t in _source_claude_web(query, llm, limit) if u not in already
        ]
        if len(found) >= min(limit, 2):
            return found[:limit]

    if not found:
        found = _source_retailer_search(query, cfg, limit)

    if not found and not answered and llm is None:
        raise SearchBlocked(
            "every search engine declined to answer (usually a rate limit). "
            "Wait a minute, or paste the product link instead."
        )
    return found[:limit]


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


def _search_each(
    products: list[str],
    cfg: dict,
    retailers: list[str] | None,
    limit: int,
    price_them: bool,
    llm=None,
    budget: dict | None = None,
) -> list[SearchResult]:
    """Find and price pages for several specific products.

    Two things keep this quick. The per-product engine searches run at the same
    time rather than one after another. And if they come up short, the slow
    source is asked *once* about all the products together - asking it per
    product turned a search into minutes.
    """
    per_product = max(1, limit // max(len(products), 1)) + 1

    def engines_for(product: str) -> tuple[str, list[tuple[str, str]]]:
        found, _answered = _source_engines(product, cfg["fetch"], [], per_product)
        return product, found

    pairs: list[tuple[str, str]] = []  # (url, product it was found for)
    titles: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=min(6, max(len(products), 1))) as pool:
        for product, urls in pool.map(engines_for, products):
            for url, title in urls:
                pairs.append((url, product))
                titles[url] = title

    thin = len(pairs) < min(limit, 2)
    if thin and llm is not None and (budget is None or budget.get("claude", 0) > 0):
        if budget is not None:
            budget["claude"] = budget.get("claude", 0) - 1
        # One question covering every product, not one per product.
        joined = ", ".join(f'"{p}"' for p in products)
        known = {u for u, _ in pairs}
        for url, _title in _source_claude_web(joined, llm, limit * 2):
            if url not in known:
                pairs.append((url, _closest_product(url, products)))

    if not price_them:
        return [
            SearchResult(
                title=titles.get(url) or product,
                url=url,
                retailer=_retailer_of(url),
                matched=product,
            )
            for url, product in pairs[:limit]
        ]

    found: list[SearchResult] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for result, (_url, product) in zip(
            pool.map(
                lambda pair: _price_one(pair[0], titles.get(pair[0], ""), cfg["fetch"]),
                pairs[: limit * 2],
            ),
            pairs[: limit * 2],
            strict=False,
        ):
            result.matched = product
            found.append(result)

    # One shop per product is enough to compare on; keep the cheapest of each.
    best_per_product: dict[str, SearchResult] = {}
    extras: list[SearchResult] = []
    for result in found:
        key = result.matched or result.url
        current = best_per_product.get(key)
        if result.price is None:
            extras.append(result)
        elif current is None or (current.price or 1e15) > result.price:
            if current is not None:
                extras.append(current)
            best_per_product[key] = result

    ordered = sorted(best_per_product.values(), key=lambda r: r.price or 1e15)
    ordered += [r for r in extras if r.price is not None]
    ordered += [r for r in extras if r.price is None]
    return ordered[:limit]


def _closest_product(url: str, products: list[str]) -> str:
    """Which of the planned products does this URL look like it belongs to?"""
    slug = re.sub(r"[^a-z0-9]+", " ", url.lower())
    best, best_hits = products[0] if products else "", 0
    for product in products:
        words = [w for w in re.split(r"[^a-z0-9]+", product.lower()) if len(w) > 2]
        hits = sum(1 for w in words if w in slug)
        if hits > best_hits:
            best, best_hits = product, hits
    return best


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
    llm=None,
    on_plan=None,
) -> list[SearchResult]:
    """Name in, priced product pages out - best match first.

    A vague ask ("thunderbolt dock") names no page anywhere, so before
    searching, Claude turns it into the specific models a knowledgeable person
    would consider. Each of those is then searched and priced for real, so a
    suggestion that is wrong or discontinued simply finds nothing.
    """
    # The slow source is worth a few runs per search, not one per planned
    # product - four models must not become four web searches.
    budget = {"claude": 3}
    planned = False
    if llm is not None and looks_vague(query):
        products, reading = plan_products(query, llm)
        if products:
            planned = True
            if on_plan:
                on_plan(products, reading)
            found = _search_each(
                products, cfg, retailers, limit, price_them, llm=llm, budget=budget
            )
            if found:
                return found

    candidates = find_urls(
        query,
        cfg["fetch"],
        retailers=retailers,
        limit=limit * 2,
        llm=llm,
        budget=budget,
    )

    # The guess above is only a guess. What settles it is whether the search
    # actually found anything to buy - if it did not, the words were a
    # category after all, whatever they looked like.
    if not candidates and llm is not None and not planned:
        products, reading = plan_products(query, llm)
        if products:
            if on_plan:
                on_plan(products, reading)
            return _search_each(
                products, cfg, retailers, limit, price_them, llm=llm, budget=budget
            )
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
