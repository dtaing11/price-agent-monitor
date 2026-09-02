"""HTTP fetching: per-host throttling, retries, browser headers, robots.txt.

Restraint here is self-interest as much as etiquette - hammering a shop is the
fastest way to earn a 403 and lose your price history. The throttle and retry
budget are always on; the robots.txt check is opt-in via
``fetch.respect_robots`` and off by default.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from pathlib import Path
from urllib import robotparser
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

_last_hit: dict[str, float] = {}
_throttle_lock = threading.Lock()
_robots: dict[str, robotparser.RobotFileParser | None] = {}


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
        import brotli  # type: ignore[import-not-found] # noqa: F401

        encodings.append("br")
    except ImportError:
        try:
            import brotlicffi  # type: ignore[import-not-found] # noqa: F401

            encodings.append("br")
        except ImportError:
            pass
    try:
        import zstandard  # type: ignore[import-not-found] # noqa: F401

        encodings.append("zstd")
    except ImportError:
        pass
    return ", ".join(encodings)


def _headers(user_agent: str) -> dict[str, str]:
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
            resp = requests.get(
                robots_url, headers=_headers(user_agent), timeout=timeout
            )
            if resp.status_code >= 400:
                _robots[host] = None  # no usable robots.txt -> allow
            else:
                rp.parse(resp.text.splitlines())
                _robots[host] = rp
        except requests.RequestException:
            _robots[host] = None
    cached = _robots[host]
    return True if cached is None else cached.can_fetch(user_agent, url)


def _throttle(url: str, min_delay: float) -> None:
    """Space out requests per host - safe to call from several threads."""
    host = _host(url)
    with _throttle_lock:
        last = _last_hit.get(host)
        wait = 0.0
        if last is not None:
            wait = min_delay + random.uniform(0, min_delay * 0.3) - (time.time() - last)
        # Reserve this host's slot before releasing the lock so concurrent
        # callers queue behind us instead of all firing at once.
        _last_hit[host] = time.time() + max(wait, 0.0)
    if wait > 0:
        time.sleep(wait)


class BrowserUnavailable(FetchError):
    pass


def browser_is_available() -> bool:
    try:
        import playwright  # type: ignore[import-not-found]  # noqa: F401

        return True
    except ImportError:
        return False


def render_page(url: str, cfg: dict, screenshot_dir: str | None = None):
    """Open the page in a real browser and report what it looks like.

    Returns a RenderedPage: the HTML, plus every price-like string with its
    computed style and position, the images ranked as a shopper would rank
    them, and optionally a screenshot. Markup alone cannot say which price is
    crossed out or which picture is the product; the browser can.
    """
    from .vision import IMAGE_PROBE_JS, PRICE_PROBE_JS, RenderedPage, SeenPrice

    try:
        from playwright.sync_api import (
            sync_playwright,  # type: ignore[import-not-found]
        )
    except ImportError as exc:
        raise BrowserUnavailable(
            "browser mode needs Playwright: pip install playwright && "
            "python3 -m playwright install chromium"
        ) from exc

    _throttle(url, float(cfg.get("min_delay_per_domain", 3.0)))
    timeout_ms = int(float(cfg.get("timeout", 25)) * 1000)
    shot_path: str | None = None

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        try:
            context = browser.new_context(
                user_agent=cfg["user_agent"],
                locale="en-US",
                viewport={"width": 1366, "height": 900},
            )
            page = context.new_page()
            page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            try:
                page.wait_for_selector(
                    "[class*=price], [id*=price], [data-testid*=price]",
                    timeout=min(timeout_ms, 8000),
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("no price selector appeared on %s: %s", url, exc)
            page.wait_for_timeout(500)

            html, final = page.content(), page.url
            try:
                raw_prices = page.evaluate(PRICE_PROBE_JS) or []
                raw_images = page.evaluate(IMAGE_PROBE_JS) or []
            except Exception as exc:  # noqa: BLE001 - probes are a bonus
                logger.debug("page probe failed on %s: %s", url, exc)
                raw_prices, raw_images = [], []

            if screenshot_dir:
                shot = Path(screenshot_dir) / f"page-{abs(hash(final)) % 10**8}.png"
                shot.parent.mkdir(parents=True, exist_ok=True)
                try:
                    # The part a shopper looks at, not the whole endless page.
                    page.screenshot(path=str(shot), full_page=False)
                    shot_path = str(shot)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("screenshot failed on %s: %s", url, exc)
        finally:
            browser.close()

    prices = [
        SeenPrice(**{k: v for k, v in item.items() if k in SeenPrice.__annotations__})
        for item in raw_prices
    ]
    return RenderedPage(
        html=html, url=final, prices=prices, images=raw_images, screenshot=shot_path
    )


def fetch_rendered(url: str, cfg: dict) -> tuple[str, str]:
    """Fetch a page through a real headless browser.

    Some retailers - Amazon and Target most notably - send an empty price
    skeleton to anything that is not a browser and fill it in with JavaScript.
    No amount of HTML parsing recovers a number that was never sent, so those
    sites get rendered properly instead.

    Needs:  pip install playwright && python3 -m playwright install chromium
    """
    try:
        from playwright.sync_api import (
            sync_playwright,  # type: ignore[import-not-found]
        )
    except ImportError as exc:
        raise BrowserUnavailable(
            "browser mode needs Playwright: pip install playwright && "
            "python3 -m playwright install chromium"
        ) from exc

    _throttle(url, float(cfg.get("min_delay_per_domain", 3.0)))
    timeout_ms = int(float(cfg.get("timeout", 25)) * 1000)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        try:
            context = browser.new_context(
                user_agent=cfg["user_agent"],
                locale="en-US",
                viewport={"width": 1366, "height": 900},
            )
            page = context.new_page()
            page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            # Prices arrive after hydration; wait for one, but never hang on it.
            try:
                page.wait_for_selector(
                    "[class*=price], [id*=price], [data-testid*=price]",
                    timeout=min(timeout_ms, 8000),
                )
            except Exception as exc:  # noqa: BLE001
                # No price element appeared - the page may genuinely not have
                # one. Carry on and let extraction decide.
                logger.debug("no price selector appeared on %s: %s", url, exc)
            page.wait_for_timeout(400)
            html, final = page.content(), page.url
        finally:
            browser.close()
    return html, final


def fetch(
    url: str, cfg: dict, session: requests.Session | None = None
) -> tuple[str, str]:
    """Return (html, final_url).  Raises FetchError on give-up."""
    ua = cfg["user_agent"]
    timeout = cfg["timeout"]

    if cfg.get("respect_robots", False) and not _robots_allows(url, ua, timeout):
        raise RobotsDenied(
            f"robots.txt disallows {url} (set fetch.respect_robots: false to ignore it)"
        )

    sess = session or requests.Session()
    last_err: str | None = None

    for attempt in range(1, int(cfg.get("max_retries", 3)) + 1):
        _throttle(url, float(cfg.get("min_delay_per_domain", 3.0)))
        try:
            resp = sess.get(
                url, headers=_headers(ua), timeout=timeout, allow_redirects=True
            )
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
                retry_after = resp.headers.get("Retry-After") or ""
                delay = (
                    float(retry_after) if retry_after.isdigit() else 2.0 * attempt**2
                )
                last_err = f"HTTP {resp.status_code}"
                time.sleep(min(delay, 60))
                continue
            raise FetchError(f"HTTP {resp.status_code} for {url}")
        time.sleep(2.0 * attempt)

    raise FetchError(f"gave up after {cfg.get('max_retries', 3)} attempts: {last_err}")
