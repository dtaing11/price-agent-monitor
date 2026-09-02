"""Turning human-written price strings into numbers.

Real pages write the same amount a dozen ways: "$1,234.56", "1.234,56 EUR",
"USD 45", "45,00 kr", "Now  1 299 ,-".  This module normalises them.
"""

from __future__ import annotations

import re

# Symbol -> ISO code.  Longest keys first when matching (see _CURRENCY_ORDER).
SYMBOLS = {
    "US$": "USD",
    "C$": "CAD",
    "CA$": "CAD",
    "A$": "AUD",
    "AU$": "AUD",
    "NZ$": "NZD",
    "HK$": "HKD",
    "S$": "SGD",
    "R$": "BRL",
    "NT$": "TWD",
    "$": "USD",
    "£": "GBP",
    "€": "EUR",
    "¥": "JPY",
    "₹": "INR",
    "₩": "KRW",
    "₺": "TRY",
    "₽": "RUB",
    "₪": "ILS",
    "₫": "VND",
    "฿": "THB",
    "₱": "PHP",
    "zł": "PLN",
    "Kč": "CZK",
    "kr": "SEK",
    "R": "ZAR",
}

CODES = {
    "USD",
    "EUR",
    "GBP",
    "JPY",
    "CAD",
    "AUD",
    "NZD",
    "CHF",
    "CNY",
    "HKD",
    "SGD",
    "INR",
    "KRW",
    "SEK",
    "NOK",
    "DKK",
    "PLN",
    "CZK",
    "HUF",
    "RON",
    "TRY",
    "RUB",
    "BRL",
    "MXN",
    "ARS",
    "ZAR",
    "AED",
    "SAR",
    "ILS",
    "THB",
    "MYR",
    "IDR",
    "PHP",
    "VND",
    "TWD",
    "UAH",
}

_CURRENCY_ORDER = sorted(SYMBOLS, key=len, reverse=True)

# A run of digits with optional , . space or non-breaking space separators.
_NUMBER_RE = re.compile(r"\d[\d.,  \s]*\d|\d")
_CODE_RE = re.compile(r"\b(" + "|".join(sorted(CODES)) + r")\b")


def detect_currency(text: str) -> str | None:
    """Pull an ISO currency code out of arbitrary text, if one is stated."""
    m = _CODE_RE.search(text.upper())
    if m:
        return m.group(1)
    for sym in _CURRENCY_ORDER:
        if sym in text:
            return SYMBOLS[sym]
    return None


def _normalise_number(raw: str) -> float | None:
    """Decide what ',' and '.' mean in a number, then produce a float.

    Rules that cover the formats seen in the wild, including Indian lakh
    grouping ("1,49,900") and European decimals ("1.234,56"):

    * both separators present -> the last one is the decimal point;
    * one separator, appearing more than once -> all thousands;
    * one separator, appearing once -> decimal if 1-2 trailing digits,
      thousands if exactly 3 ("1,234" is twelve hundred, not 1.234).
    """
    s = re.sub(r"[\u00a0\u202f\s]", "", raw).strip(".,")
    if not s or not any(ch.isdigit() for ch in s):
        return None

    n_comma, n_dot = s.count(","), s.count(".")
    dec_sep = None

    if n_comma and n_dot:
        dec_sep = "," if s.rfind(",") > s.rfind(".") else "."
    elif n_comma or n_dot:
        sep = "," if n_comma else "."
        if (n_comma + n_dot) == 1 and len(s) - s.rfind(sep) - 1 in (1, 2):
            dec_sep = sep

    if dec_sep:
        digits, frac = s.rsplit(dec_sep, 1)
    else:
        digits, frac = s, ""

    digits = re.sub(r"[.,]", "", digits) or "0"
    if not digits.isdigit() or (frac and not frac.isdigit()):
        return None
    try:
        return float(f"{digits}.{frac}") if frac else float(digits)
    except ValueError:
        return None


def parse_price(text: str) -> tuple[float | None, str | None]:
    """Extract (amount, currency) from a price-ish string.

    Returns (None, ...) when the text holds no plausible amount.
    """
    if text is None:
        return None, None
    text = str(text).replace("−", "-").strip()
    if not text:
        return None, None

    currency = detect_currency(text)
    # "Save 20% - $79.99": a percentage is never the price.
    text = re.sub(r"\d[\d.,]*\s*%", " ", text)

    # Ranges ("$20 - $30") and "from" prices: take the first amount.
    for m in _NUMBER_RE.finditer(text):
        value = _normalise_number(m.group(0))
        if value is not None and value > 0:
            return round(value, 2), currency
    return None, currency


def format_price(amount: float | None, currency: str | None) -> str:
    if amount is None:
        return "n/a"
    sym = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "INR": "₹"}.get(
        currency or ""
    )
    if sym:
        return f"{sym}{amount:,.2f}"
    return f"{amount:,.2f} {currency}" if currency else f"{amount:,.2f}"
