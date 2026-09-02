"""Merging results from several providers into one ranking.

Providers disagree about order and repeat each other's hits under slightly
different URLs. Canonicalising the URL collapses the duplicates; reciprocal
rank fusion merges the orders without needing their scores to be comparable,
which they never are.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .models import SearchResult

# Parameters that identify a click, not a page.
TRACKING = re.compile(
    r"^(utm_|fbclid$|gclid$|msclkid$|mc_cid$|mc_eid$|igshid$|ref$|ref_$|_ga$"
    r"|yclid$|dclid$|twclid$|s_kwcid$|gbraid$|wbraid$|srsltid$)",
    re.IGNORECASE,
)


def canonical_url(url: str) -> str:
    """One spelling per page, so duplicates collapse.

    Drops tracking parameters, the fragment, a leading www and a trailing
    slash, and lowercases the host - none of which change what is served.
    """
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return url
    if not parsed.scheme:
        return url

    host = parsed.netloc.lower()
    host = host.removeprefix("www.")
    if (parsed.scheme == "https" and host.endswith(":443")) or (
        parsed.scheme == "http" and host.endswith(":80")
    ):
        host = host.rsplit(":", 1)[0]

    query = urlencode(
        [
            (k, v)
            for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if not TRACKING.match(k)
        ]
    )
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme, host, path, parsed.params, query, ""))


def reciprocal_rank_fusion(
    rankings: list[list[SearchResult]], k: int = 60, limit: int | None = None
) -> list[SearchResult]:
    """Merge ranked lists by 1/(k+rank), the standard RRF.

    Scores from different providers are not comparable, but positions are: a
    result several providers put near the top outranks one that a single
    provider loved.
    """
    scores: dict[str, float] = {}
    best: dict[str, SearchResult] = {}
    found_by: dict[str, set[str]] = {}

    for ranking in rankings:
        for position, result in enumerate(ranking, start=1):
            key = canonical_url(result.url)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + position)
            found_by.setdefault(key, set()).add(result.provider)
            keep = best.get(key)
            if keep is None or (not keep.snippet and result.snippet):
                best[key] = result

    ordered = sorted(scores.items(), key=lambda kv: -kv[1])
    out: list[SearchResult] = []
    for rank, (key, _score) in enumerate(ordered, start=1):
        source = best[key]
        providers = sorted(found_by[key])
        out.append(
            source.model_copy(
                update={
                    "url": key,
                    "rank": rank,
                    "provider": "+".join(providers)
                    if len(providers) > 1
                    else source.provider,
                }
            )
        )
        if limit and len(out) >= limit:
            break
    return out
