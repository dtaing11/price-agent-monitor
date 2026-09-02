"""Making near-identical queries share a cache key.

An agent loop asks the same thing many ways: "Sony WH-1000XM5 price",
"sony wh-1000xm5  price?", "price of Sony WH-1000XM5". Those are one question,
and they should cost one call. Normalising before hashing is the cheapest win
in the whole layer, which is why it is built first.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

# Words that never change which results come back.
STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "for",
        "to",
        "in",
        "on",
        "at",
        "by",
        "with",
        "from",
        "is",
        "are",
        "be",
        "am",
        "was",
        "were",
        "do",
        "does",
        "did",
        "how",
        "what",
        "which",
        "where",
        "when",
        "who",
        "whom",
        "whose",
        "why",
        "can",
        "could",
        "should",
        "would",
        "will",
        "shall",
        "may",
        "might",
        "must",
        "i",
        "me",
        "my",
        "we",
        "our",
        "you",
        "your",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "there",
        "here",
        "please",
        "find",
        "show",
        "get",
        "me",
        "buy",
        "price",
        "prices",
        "cost",
        "cheapest",
        "best",
        "deal",
        "deals",
        "online",
        "store",
        "shop",
        "buying",
        "purchase",
    ]
)

_PUNCT = re.compile(r"[^\w\s-]", re.UNICODE)
_SPACE = re.compile(r"\s+")


def normalize(query: str, drop_stopwords: bool = True) -> str:
    """Fold a query to its meaning-bearing core.

    Lowercase, strip accents and punctuation, collapse whitespace, drop
    stopwords, and sort nothing - word order can matter ("dock station" vs
    "station dock" are the same, but "iphone case" vs "case iphone" reads the
    same too, so tokens are sorted only for the cache *key*, not here).
    """
    if not query:
        return ""
    text = unicodedata.normalize("NFKD", str(query))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = _PUNCT.sub(" ", text)
    tokens = [t for t in _SPACE.split(text) if t]
    if drop_stopwords:
        kept = [t for t in tokens if t not in STOPWORDS]
        # Never normalise a query out of existence - "the best of the best"
        # still has to search for something.
        tokens = kept or tokens
    return " ".join(tokens)


def tokens(query: str) -> list[str]:
    return normalize(query).split()


def cache_key(query: str, count: int = 10, lang: str = "en") -> str:
    """A stable key for a query, insensitive to wording that does not matter.

    Tokens are sorted here (and only here) so "dock thunderbolt" and
    "thunderbolt dock" share one entry, while the query sent upstream keeps its
    natural order.
    """
    core = " ".join(sorted(set(tokens(query))))
    raw = f"{core}|{count}|{lang}"
    return "q:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
