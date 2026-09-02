"""Per-retailer knowledge: selectors, quirks, URL canonicalisation, bot walls.

The generic cascade in ``extract`` handles most of the web. Big retailers are
worth special-casing anyway: their markup is stable, their pages are noisy
(bundles, "customers also bought", subscription pricing), and knowing the right
selector up front avoids both a wrong answer and an LLM call.

Rules are hints, never a hard requirement - if a selector stops matching after
a redesign, extraction falls through to the generic cascade and then to Claude,
which learns a new selector.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


@dataclass(frozen=True)
class SiteRule:
    name: str
    domains: tuple[str, ...]
    price: tuple[str, ...] = ()
    title: tuple[str, ...] = ()
    out_of_stock: tuple[str, ...] = ()
    in_stock: tuple[str, ...] = ()
    currency: str | None = None
    bot_protection: str = "low"  # low | medium | high
    note: str = ""
    strip_query: bool = True
    keep_params: tuple[str, ...] = field(default_factory=tuple)


RULES: tuple[SiteRule, ...] = (
    SiteRule(
        name="Amazon",
        domains=(
            "amazon.com",
            "amazon.co.uk",
            "amazon.de",
            "amazon.fr",
            "amazon.it",
            "amazon.es",
            "amazon.ca",
            "amazon.com.au",
            "amazon.co.jp",
            "amazon.in",
            "amazon.com.mx",
            "amazon.nl",
            "amazon.se",
            "amazon.pl",
            "amazon.com.br",
        ),
        price=(
            "#corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen",
            "#corePrice_feature_div .a-price .a-offscreen",
            "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
            ".priceToPay .a-offscreen",
            "#price_inside_buybox",
            "#priceblock_ourprice",
            "#priceblock_dealprice",
            "#newBuyBoxPrice",
            "#apex_desktop .a-price .a-offscreen",
            ".a-price[data-a-color='price'] .a-offscreen",
        ),
        title=("#productTitle", "#title span"),
        out_of_stock=("#outOfStock", "#availability .a-color-price"),
        in_stock=("#add-to-cart-button", "#buy-now-button"),
        bot_protection="high",
        note="Amazon serves a CAPTCHA to most non-browser traffic. If checks "
        "fail, use the Product Advertising API or a browser fetch.",
    ),
    SiteRule(
        name="Walmart",
        domains=("walmart.com", "walmart.ca"),
        price=(
            "span[itemprop='price']",
            "[data-testid='price-wrap'] span[itemprop='price']",
            "[data-seo-id='hero-price']",
            "[data-automation-id='product-price'] .inline-flex span",
        ),
        title=("#main-title", "h1[itemprop='name']"),
        out_of_stock=("[data-automation-id='out-of-stock']",),
        bot_protection="high",
        note="Walmart embeds the authoritative price in its __NEXT_DATA__ JSON, "
        "which the embedded-JSON extractor reads.",
    ),
    SiteRule(
        name="Target",
        domains=("target.com",),
        price=("[data-test='product-price']", "[data-test='product-price-value']"),
        title=("[data-test='product-title']", "h1[data-test='product-title']"),
        out_of_stock=("[data-test='oosDeliveryMessage']",),
        bot_protection="high",
    ),
    SiteRule(
        name="Best Buy",
        domains=("bestbuy.com", "bestbuy.ca"),
        price=(
            "[data-testid='customer-price'] span",
            ".priceView-customer-price span",
            ".priceView-hero-price span",
        ),
        title=(".sku-title h1", "h1.heading-5"),
        out_of_stock=(".fulfillment-add-to-cart-button button[disabled]",),
        bot_protection="high",
    ),
    SiteRule(
        name="eBay",
        domains=("ebay.com", "ebay.co.uk", "ebay.de", "ebay.com.au", "ebay.ca"),
        price=(
            ".x-price-primary span.ux-textspans",
            "#prcIsum",
            "#mm-saleDscPrc",
            "[data-testid='x-price-primary'] span",
        ),
        title=(".x-item-title__mainTitle span", "#itemTitle"),
        out_of_stock=("#w1-8-_msg",),
        note="Auction listings change price by bid; 'Buy It Now' listings are "
        "the ones worth watching.",
        keep_params=("hash",),
    ),
    SiteRule(
        name="Etsy",
        domains=("etsy.com",),
        price=(
            "[data-buy-box-region='price'] p.wt-text-title-larger",
            "[data-selector='price-only'] .wt-text-title-03",
            "p[data-buy-box-region='price']",
        ),
        title=("h1[data-buy-box-listing-title]", "h1.wt-text-body-01"),
        bot_protection="medium",
    ),
    SiteRule(
        name="Newegg",
        domains=("newegg.com", "newegg.ca"),
        price=(".price-current", "li.price-current"),
        title=("h1.product-title",),
        out_of_stock=(".product-inventory strong",),
        bot_protection="medium",
    ),
    SiteRule(
        name="The Home Depot",
        domains=("homedepot.com",),
        price=(
            "[data-testid='product-price'] .sui-font-display",
            ".price-format__main-price",
        ),
        title=("h1.product-details__title", "h1"),
        bot_protection="high",
    ),
    SiteRule(
        name="Lowe's",
        domains=("lowes.com",),
        price=(
            "[data-selector='splp-prd-bd-prc'] .screen-reader-only",
            "[itemprop='price']",
        ),
        title=("h1.pdp-title", "h1"),
        bot_protection="high",
    ),
    SiteRule(
        name="Costco",
        domains=("costco.com", "costco.co.uk", "costco.ca"),
        price=("[automation-id='productPriceOutput']", ".your-price .value"),
        title=("h1[automation-id='productName']", "h1"),
        bot_protection="high",
    ),
    SiteRule(
        name="Wayfair",
        domains=("wayfair.com", "wayfair.co.uk"),
        price=(
            "[data-enzyme-id='PriceBlock'] span",
            "span[data-test-id='PriceDisplay']",
        ),
        title=("h1[data-enzyme-id='ProductTitle']", "h1"),
        bot_protection="high",
    ),
    SiteRule(
        name="Chewy",
        domains=("chewy.com",),
        price=("[data-testid='current-price']", ".ga-eec__price"),
        title=("#product-title h1", "h1"),
    ),
    SiteRule(
        name="B&H Photo",
        domains=("bhphotovideo.com",),
        price=("[data-selenium='pricingPrice']",),
        title=("[data-selenium='productTitle']", "h1"),
        bot_protection="medium",
    ),
    SiteRule(
        name="GameStop",
        domains=("gamestop.com",),
        price=(".actual-price", "[itemprop='price']"),
        title=("h1.product-name", "h1"),
    ),
    SiteRule(
        name="Steam",
        domains=("store.steampowered.com",),
        price=(
            ".game_purchase_action .discount_final_price",
            ".game_purchase_action .game_purchase_price",
        ),
        title=("#appHubAppName", ".apphub_AppName"),
        note="Steam prices are region-dependent; the value follows the store "
        "country of the IP the check runs from.",
    ),
    SiteRule(
        name="IKEA",
        domains=("ikea.com",),
        price=(".pip-temp-price__integer", "[data-testid='price']"),
        title=(".pip-header-section__title--big", "h1"),
    ),
    SiteRule(
        name="Argos",
        domains=("argos.co.uk",),
        price=("[data-test='product-price-primary']", "h2[data-test='product-price']"),
        title=("h1[data-test='product-title']", "h1"),
        currency="GBP",
    ),
    SiteRule(
        name="Currys",
        domains=("currys.co.uk",),
        price=(".product-price_price .value", "[data-product-price]"),
        title=("h1.product-name", "h1"),
        currency="GBP",
    ),
    SiteRule(
        name="John Lewis",
        domains=("johnlewis.com",),
        price=("[data-testid='price'] span", ".price--large"),
        title=("h1[data-testid='product:title']", "h1"),
        currency="GBP",
    ),
    SiteRule(
        name="Zalando",
        domains=("zalando.co.uk", "zalando.de", "zalando.fr", "zalando.nl"),
        price=("[data-testid='pdp-current-price']", "span._4sa1cA"),
        title=("h1 span", "h1"),
    ),
    SiteRule(
        name="AliExpress",
        domains=("aliexpress.com", "aliexpress.us"),
        price=(".product-price-current", "[class*='Price_uniformBanner']"),
        title=("h1[data-pl='product-title']", "h1"),
        bot_protection="medium",
    ),
    SiteRule(
        name="Apple",
        domains=("apple.com",),
        price=(".rc-prices-fullprice", "[data-autom='full-price']"),
        title=("h1", ".pd-hero-title"),
    ),
    SiteRule(
        name="Nike",
        domains=("nike.com",),
        price=("[data-testid='currentPrice-container']", "#price-container span"),
        title=("h1#pdp_product_title", "h1"),
        bot_protection="medium",
    ),
    SiteRule(
        name="Decathlon",
        domains=("decathlon.com", "decathlon.co.uk", "decathlon.fr"),
        price=("[data-testid='price'] span", ".prc__active-price"),
        title=("h1", ".product-title"),
    ),
)

_DOMAIN_INDEX: dict[str, SiteRule] = {d: r for r in RULES for d in r.domains}

# Pages that are really a bot wall wearing a product page's clothes.
BLOCK_MARKERS = (
    "robot check",
    "enter the characters you see below",
    "type the characters you see in this image",
    "pardon our interruption",
    "access denied",
    "are you a human",
    "verify you are a human",
    "unusual traffic from your computer",
    "px-captcha",
    "cf-browser-verification",
    "checking your browser before accessing",
    "request unsuccessful. incapsula",
)

TRACKING_PARAMS = re.compile(
    r"^(utm_|ref_?$|ref=|gclid|fbclid|msclkid|_encoding|psc$|pd_rd_|pf_rd_|"
    r"th$|linkCode|tag$|creative|creativeASIN|ascsubtag|sr$|qid$|keywords$|"
    r"sprefix|crid|dib|dib_tag|content-id|smid|irgwc|source|cm_sp)",
    re.IGNORECASE,
)


def match(url: str) -> SiteRule | None:
    """Find the rule for a URL, matching parent domains too (www., country subs)."""
    host = urlparse(url).netloc.lower().split(":")[0]
    host = host.removeprefix("www.")
    if host in _DOMAIN_INDEX:
        return _DOMAIN_INDEX[host]
    parts = host.split(".")
    for i in range(1, len(parts) - 1):
        candidate = ".".join(parts[i:])
        if candidate in _DOMAIN_INDEX:
            return _DOMAIN_INDEX[candidate]
    return None


def canonical_url(url: str) -> str:
    """Strip tracking cruft so the same product is not watched twice.

    Amazon collapses to /dp/<ASIN>, which is the stable form of any product URL.
    """
    parsed = urlparse(url)
    rule = match(url)

    path = parsed.path
    if rule and rule.name == "Amazon":
        asin = re.search(
            r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})", path, re.IGNORECASE
        )
        if asin:
            return urlunparse(
                (
                    parsed.scheme,
                    parsed.netloc,
                    f"/dp/{asin.group(1).upper()}",
                    "",
                    "",
                    "",
                )
            )

    query = [
        (k, v)
        for k, v in parse_qsl(parsed.query)
        if not TRACKING_PARAMS.match(k)
        and (not rule or not rule.strip_query or k in (rule.keep_params or ()))
    ]
    return urlunparse(
        (parsed.scheme, parsed.netloc, path, parsed.params, urlencode(query), "")
    )


def looks_blocked(html: str) -> str | None:
    """Return a human explanation when a page is a bot wall, else None."""
    head = html[:8000].lower()
    for marker in BLOCK_MARKERS:
        if marker in head:
            return f"the site returned a bot check ({marker!r}), not the product page"
    if len(html.strip()) < 700 and "captcha" in head:
        return "the site returned a CAPTCHA page"
    return None


def supported_sites() -> list[tuple[str, str, str]]:
    """(retailer, primary domain, bot-protection) for display in the UI."""
    return sorted((r.name, r.domains[0], r.bot_protection) for r in RULES)
