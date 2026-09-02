"""Polite HTTP fetching: per-host throttling, robots.txt, retries, browser headers.

Being well behaved is not just etiquette - hammering a shop is the fastest way
to earn a 403 and lose your price history.
"""

from __future__ import annotations

import random
import time
import urllib.robotparser as robotparser
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse

import requests

_last_hit: Dict[str, float] = {}
_robots: Dict[str, Optional[robotparser.RobotFileParser]] = {}


class FetchError(RuntimeError):
    pass


class RobotsDenied(FetchError):
    pass


def _host(url: str) -> str:
    return urlparse(url).netloc.lower()


def _accept_encoding() -> str:
    """Only advertise what this install can actually decode.

    Claiming br/zstd without the codec installed gets you a body of binary
    noise that silently parses to an empty page.
    """
    encodings = ["gzip", "deflate"]
    try:
        import brotli  # noqa: F401
        encodings.append("br")
    except ImportError:
        try:
            import brotlicffi  # noqa: F401
            encodings.append("br")
        except ImportError:
            pass
    try:
        import zstandard  # noqa: F401
        encodings.append("zstd")
    except ImportError:
        pass
    return ", ".join(encodings)


def _headers(user_agent: str) -> Dict[str, str]:
    return {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": _accept_encoding(),
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }


def _robots_allows(url: str, user_agent: str, timeout: float) -> bool:
    host = _host(url)
    if host not in _robots:
        parsed = urlparse(url)
        rp = robotparser.RobotFileParser()
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        try:
            resp = requests.get(robots_url, headers=_headers(user_agent), timeout=timeout)
            if resp.status_code >= 400:
                _robots[host] = None          # no usable robots.txt -> allow
            else:
                rp.parse(resp.text.splitlines())
                _robots[host] = rp
        except requests.RequestException:
            _robots[host] = None
    rp = _robots[host]
    return True if rp is None else rp.can_fetch(user_agent, url)


def _throttle(url: str, min_delay: float) -> None:
    host = _host(url)
    last = _last_hit.get(host)
    if last is not None:
        wait = min_delay + random.uniform(0, min_delay * 0.3) - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
    _last_hit[host] = time.time()


def fetch(url: str, cfg: dict, session: Optional[requests.Session] = None) -> Tuple[str, str]:
    """Return (html, final_url).  Raises FetchError on give-up."""
    ua = cfg["user_agent"]
    timeout = cfg["timeout"]

    if cfg.get("respect_robots", True) and not _robots_allows(url, ua, timeout):
        raise RobotsDenied(f"robots.txt disallows {url} (set fetch.respect_robots: false to override)")

    sess = session or requests.Session()
    last_err: Optional[str] = None

    for attempt in range(1, int(cfg.get("max_retries", 3)) + 1):
        _throttle(url, float(cfg.get("min_delay_per_domain", 3.0)))
        try:
            resp = sess.get(url, headers=_headers(ua), timeout=timeout, allow_redirects=True)
        except requests.RequestException as exc:
            last_err = f"{type(exc).__name__}: {exc}"
        else:
            if resp.status_code == 200:
                # requests falls back to ISO-8859-1 whenever the server omits a
                # charset, which turns UTF-8 pages into mojibake ("Â£53.74").
                # Only trust the header when it actually declares one.
                if "charset" not in resp.headers.get("Content-Type", "").lower():
                    resp.encoding = resp.apparent_encoding or resp.encoding
                return resp.text, resp.url
            if resp.status_code in (429, 500, 502, 503, 504):
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if (retry_after or "").isdigit() else 2.0 * attempt ** 2
                last_err = f"HTTP {resp.status_code}"
                time.sleep(min(delay, 60))
                continue
            raise FetchError(f"HTTP {resp.status_code} for {url}")
        time.sleep(2.0 * attempt)

    raise FetchError(f"gave up after {cfg.get('max_retries', 3)} attempts: {last_err}")
