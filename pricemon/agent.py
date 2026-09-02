"""The agent loop: fetch, extract, decide, remember, alert."""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests

from . import extract as extract_mod
from . import sites
from .fetcher import FetchError, browser_is_available, fetch, fetch_rendered
from .llm import LLM, LLMUnavailable
from .models import Alert, CheckResult, Extraction, Product, utcnow
from .money import format_price
from .storage import Store

# Below this, the deterministic guess is not trustworthy enough to act on.
LLM_CONFIDENCE_FLOOR = 0.75


class Agent:
    def __init__(self, store: Store, cfg: dict, verbose: bool = True):
        self.store = store
        self.cfg = cfg
        self.verbose = verbose
        self.llm = LLM(cfg["llm"])
        # requests.Session is not safe to share between threads, so each worker
        # gets its own. Connection pooling still applies within a thread.
        self._local = threading.local()
        # Browsers and LLM calls are far heavier than an HTTP fetch; letting
        # every worker start Chromium at once would swamp the machine.
        self._heavy = threading.Semaphore(
            max(1, int(cfg["fetch"].get("heavy_workers", 2)))
        )

    @property
    def session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            self._local.session = session
        return session

    def log(self, msg: str) -> None:
        """Collect output per product, so parallel checks do not interleave."""
        buffer = getattr(self._local, "buffer", None)
        if buffer is not None:
            buffer.append(msg)
        elif self.verbose:
            print(msg)

    # ------------------------------------------------------------------
    def scrape(
        self, url: str, product: Product | None = None, use_llm: bool = True
    ) -> tuple[Extraction, list[Extraction]]:
        """Fetch a page and extract a price, escalating as needed.

        Escalation ladder: plain HTTP -> headless browser (for sites that render
        prices client-side) -> Claude. Each step costs more, so each only runs
        when the cheaper one came up short.
        """
        mode = str(self.cfg["fetch"].get("browser", "auto")).lower()
        rule = sites.match(url)
        render_first = mode == "always" or (
            mode == "auto"
            and rule is not None
            and rule.bot_protection == "high"
            and browser_is_available()
        )

        if render_first:
            try:
                with self._heavy:
                    html, final_url = fetch_rendered(url, self.cfg["fetch"])
                self.log("  rendered in a headless browser")
            except FetchError as exc:
                self.log(f"  browser fetch failed ({exc}); falling back to plain HTTP")
                html, final_url = fetch(url, self.cfg["fetch"], session=self.session)
        else:
            html, final_url = fetch(url, self.cfg["fetch"], session=self.session)

        blocked = sites.looks_blocked(html)
        if blocked and mode != "never" and not render_first and browser_is_available():
            self.log(f"  {blocked}; retrying in a headless browser")
            try:
                html, final_url = fetch_rendered(url, self.cfg["fetch"])
                blocked = sites.looks_blocked(html)
            except FetchError as exc:
                self.log(f"  browser fetch failed: {exc}")
        if blocked:
            raise FetchError(blocked)

        best, candidates = extract_mod.extract(
            html,
            selector=product.selector if product else None,
            learned_selector=product.learned_selector if product else None,
            url=final_url,
        )

        # Nothing in the HTML? The page may fill prices in with JavaScript.
        if (
            not best.ok
            and not render_first
            and mode != "never"
            and browser_is_available()
        ):
            self.log("  no price in the raw HTML - re-fetching with a browser")
            try:
                html, final_url = fetch_rendered(url, self.cfg["fetch"])
            except FetchError as exc:
                self.log(f"  browser fetch failed: {exc}")
            else:
                best, candidates = extract_mod.extract(
                    html,
                    selector=product.selector if product else None,
                    learned_selector=product.learned_selector if product else None,
                    url=final_url,
                )

        needs_help = (not best.ok) or best.confidence < LLM_CONFIDENCE_FLOOR
        if needs_help and use_llm and self.llm.available:
            self.log(
                f"  deterministic result weak ({best.method}, "
                f"conf {best.confidence:.2f}) - asking Claude..."
            )
            try:
                with self._heavy:
                    llm_ex = self.llm.extract(html, final_url, candidates)
            except (LLMUnavailable, Exception) as exc:  # noqa: BLE001
                self.log(f"  LLM extraction unavailable: {exc}")
            else:
                llm_price = llm_ex.price
                if llm_price is not None:
                    # Trust but verify: if Claude's selector really resolves to
                    # that price, keep it for next time.
                    if llm_ex.selector:
                        check = extract_mod.from_selector(
                            extract_mod._soup(html), llm_ex.selector
                        )
                        if not check or abs((check.price or 0) - llm_price) > 0.01:
                            llm_ex.selector = None
                            llm_ex.note += " [selector did not verify - not saved]"
                    if llm_ex.in_stock is None:
                        llm_ex.in_stock = best.in_stock
                    llm_ex.title = llm_ex.title or best.title
                    candidates.insert(0, llm_ex)
                    best = llm_ex
        return best, candidates

    # ------------------------------------------------------------------
    def _context(self, p: Product, price: float | None, cur: str | None) -> str:
        """A short verdict on whether this price is actually worth acting on.

        "Dropped 12%" does not tell you whether to buy. "Lowest in 47 days"
        does.
        """
        if price is None:
            return ""
        ctx = self.store.price_context(p, price)
        if ctx.get("points", 0) < 2:
            return ""

        low = ctx.get("low")
        days = ctx.get("days") or 0
        span = f"in {days} days" if days >= 2 else "so far"

        if low is not None and price <= low + 0.005:
            return f" — lowest {span}"
        if low:
            above = (price - low) / low * 100
            if above <= 5:
                return f" — within {above:.0f}% of its lowest {span} ({format_price(low, cur)})"
            return f" — still {above:.0f}% above its lowest {span} ({format_price(low, cur)})"
        return ""

    def _decide(self, p: Product, ex: Extraction) -> list[Alert]:
        alerts: list[Alert] = []
        cur = ex.currency or p.currency
        now = format_price(ex.price, cur)
        # A notification should say "BILLY Bookcase", not "billy-bookcase-white-80x28x202-cm".
        label = (p.title or p.name).strip()
        if len(label) > 62:
            label = label[:60].rsplit(" ", 1)[0] + "…"
        acfg = self.cfg["alerts"]

        if ex.price is not None:
            if p.target_price is not None and ex.price <= p.target_price:
                # Only speak when something changed: the price just crossed into
                # target territory, or it fell further while already there.
                # Otherwise a cheap product would nag on every single run.
                crossed = p.last_price is None or p.last_price > p.target_price
                new_low = p.last_price is not None and ex.price < p.last_price
                if crossed or new_low:
                    alerts.append(
                        Alert(
                            url=p.url,
                            currency=cur,
                            kind="target_hit",
                            product=p.name,
                            price=ex.price,
                            message=f"{label} hit your target: {now} "
                            f"(target {format_price(p.target_price, cur)})"
                            f"{self._context(p, ex.price, cur)}",
                        )
                    )

            if p.last_price is not None and ex.price < p.last_price:
                drop = (p.last_price - ex.price) / p.last_price * 100
                if drop >= float(acfg.get("drop_pct", 5.0)) and not alerts:
                    alerts.append(
                        Alert(
                            url=p.url,
                            currency=cur,
                            kind="price_drop",
                            product=p.name,
                            price=ex.price,
                            message=f"{label} dropped {drop:.1f}%: "
                            f"{format_price(p.last_price, cur)} → {now}"
                            f"{self._context(p, ex.price, cur)}",
                        )
                    )

            if (
                p.last_price is not None
                and ex.price > p.last_price
                and acfg.get("notify_price_rise")
            ):
                rise = (ex.price - p.last_price) / p.last_price * 100
                alerts.append(
                    Alert(
                        url=p.url,
                        currency=cur,
                        kind="price_rise",
                        product=p.name,
                        price=ex.price,
                        message=f"{label} rose {rise:.1f}%: "
                        f"{format_price(p.last_price, cur)} → {now}",
                    )
                )

        if acfg.get("notify_stock_change", True) and ex.in_stock is not None:
            if p.last_in_stock is False and ex.in_stock is True:
                alerts.append(
                    Alert(
                        url=p.url,
                        currency=cur,
                        kind="back_in_stock",
                        product=p.name,
                        price=ex.price,
                        message=f"{label} is back in stock at {now}"
                        f"{self._context(p, ex.price, cur)}",
                    )
                )
            elif p.last_in_stock is True and ex.in_stock is False:
                alerts.append(
                    Alert(
                        url=p.url,
                        currency=cur,
                        kind="out_of_stock",
                        product=p.name,
                        price=ex.price,
                        message=f"{label} went out of stock",
                    )
                )
        return alerts

    # ------------------------------------------------------------------
    def _implausible(self, p: Product, ex: Extraction) -> str | None:
        """Is this price change too large to take at face value?

        The common cause of a huge overnight "drop" is not a sale - it is a
        redesigned page where the selector now points at an accessory, a
        monthly instalment or a delivery charge. Alerting on that trains you to
        ignore alerts, which costs more than the deal you miss.
        """
        if p.last_price is None or ex.price is None or p.last_price <= 0:
            return None
        limit = float(self.cfg["alerts"].get("implausible_pct", 65.0))
        if limit <= 0:
            return None
        change = abs(ex.price - p.last_price) / p.last_price * 100
        if change < limit:
            return None
        direction = "fall" if ex.price < p.last_price else "jump"
        return (
            f"implausible {change:.0f}% {direction} "
            f"({format_price(p.last_price, p.currency)} → "
            f"{format_price(ex.price, ex.currency or p.currency)})"
        )

    def _second_opinion(
        self, p: Product, ex: Extraction
    ) -> tuple[Extraction, str | None]:
        """Re-read the page, asking Claude, before believing a wild change.

        Returns the reading to record and the reason to stay quiet, if any. A
        confirmed change is a real one - a genuine 70% clearance still alerts,
        it just has to survive a second look first.
        """
        if not self.llm.available:
            return ex, self._implausible(p, ex)
        self.log("  price moved implausibly - re-reading the page to confirm")
        try:
            html, final_url = fetch(p.url, self.cfg["fetch"], session=self.session)
            candidates = extract_mod.extract(
                html,
                selector=p.selector,
                learned_selector=p.learned_selector,
                url=final_url,
            )[1]
            second = self.llm.extract(html, final_url, candidates)
        except (FetchError, LLMUnavailable) as exc:
            self.log(f"  could not confirm ({exc})")
            return ex, self._implausible(p, ex)
        except Exception as exc:  # noqa: BLE001 - a failed check must not kill the run
            self.log(f"  could not confirm ({type(exc).__name__}: {exc})")
            return ex, self._implausible(p, ex)

        if second.price is None:
            return ex, self._implausible(p, ex)
        if abs(second.price - (ex.price or 0)) <= max(0.01, second.price * 0.01):
            self.log(f"  confirmed at {format_price(second.price, second.currency)}")
            if second.selector:
                p.learned_selector = second.selector
            return second, None

        # The two readings disagree: the page changed shape. Keep Claude's
        # reading, which was told to find the price a shopper actually pays.
        self.log(
            f"  readings disagree ({format_price(ex.price, ex.currency)} vs "
            f"{format_price(second.price, second.currency)}) - taking the "
            f"second and staying quiet this round"
        )
        if second.selector:
            p.learned_selector = second.selector
        return second, self._implausible(p, second)

    def check(self, p: Product, use_llm: bool = True) -> CheckResult:
        lines: list[str] = []
        self._local.buffer = lines
        try:
            return self._check(p, use_llm=use_llm, lines=lines)
        finally:
            self._local.buffer = None
            if self.verbose:
                for line in lines:
                    print(line)

    def _check(
        self, p: Product, use_llm: bool = True, lines: list[str] | None = None
    ) -> CheckResult:
        self.log(f"→ {p.title or p.name}")
        try:
            ex, _ = self.scrape(p.url, product=p, use_llm=use_llm)
        except FetchError as exc:
            return self._record_failure(p, str(exc))
        except Exception as exc:  # noqa: BLE001
            return self._record_failure(p, f"{type(exc).__name__}: {exc}")

        if not ex.ok:
            return self._record_failure(p, "no price found on page")

        suspect = self._implausible(p, ex)
        if suspect:
            ex, suspect = self._second_opinion(p, ex)

        alerts = [] if suspect else self._decide(p, ex)
        if suspect:
            self.log(f"  {suspect} — recorded, but not alerting on it")
            alerts = [
                Alert(
                    kind="needs_check",
                    product=p.name,
                    price=ex.price,
                    currency=ex.currency or p.currency,
                    url=p.url,
                    message=f"{p.title or p.name}: {suspect}",
                )
            ]
        self.store.record(p, ex)

        # Learn: keep a verified selector so the next run skips the LLM.
        if ex.method == "llm" and ex.selector:
            p.learned_selector = ex.selector
            self.log(f"  learned selector: {ex.selector}")
        elif (
            ex.method == "heuristic" and ex.confidence >= 0.5 and not p.learned_selector
        ):
            p.learned_selector = ex.selector

        p.title = ex.title or p.title
        p.image = ex.image or p.image
        p.last_price = ex.price
        p.last_in_stock = ex.in_stock
        p.last_checked = utcnow()
        p.currency = ex.currency or p.currency
        p.fail_count = 0
        self.store.update_product(p)
        if alerts:
            self.store.record_alerts(p, alerts)

        stock = (
            ""
            if ex.in_stock is None
            else (" · in stock" if ex.in_stock else " · OUT OF STOCK")
        )
        self.log(
            f"  {format_price(ex.price, ex.currency or p.currency)}"
            f" via {ex.method} (conf {ex.confidence:.2f}){stock}"
        )
        return CheckResult(
            product=p, extraction=ex, alerts=alerts, log=list(lines or [])
        )

    def _record_failure(self, p: Product, error: str) -> CheckResult:
        p.fail_count += 1
        p.last_checked = utcnow()
        self.store.update_product(p)
        self.store.record(p, Extraction(method="error"), error=error)
        self.log(f"  failed: {error}")

        alerts: list[Alert] = []
        streak = int(self.cfg["alerts"].get("fail_streak_alert", 3))
        if streak and p.fail_count == streak:
            alerts.append(
                Alert(
                    url=p.url,
                    kind="error",
                    product=p.name,
                    price=None,
                    message=f"{p.title or p.name} failed {p.fail_count} "
                    f"checks in a row: {error}",
                )
            )
            self.store.record_alerts(p, alerts)
        return CheckResult(
            product=p,
            extraction=Extraction(method="error"),
            alerts=alerts,
            error=error,
            log=list(getattr(self._local, "buffer", None) or []),
        )

    # ------------------------------------------------------------------
    def check_all(
        self,
        names: list[str] | None = None,
        use_llm: bool = True,
        on_progress: Callable[[CheckResult], None] | None = None,
    ) -> list[CheckResult]:
        products = (
            [self.store.get_product(n) for n in names]
            if names
            else self.store.list_products(active_only=True)
        )
        products = [p for p in products if p]

        workers = max(1, int(self.cfg["fetch"].get("workers", 6)))
        results: list[CheckResult] = []

        if workers == 1 or len(products) <= 1:
            for p in products:
                res = self.check(p, use_llm=use_llm)
                results.append(res)
                if on_progress:
                    on_progress(res)
        else:
            # Several sites at once. Requests to the *same* host still queue
            # behind the fetcher's per-domain throttle, so this speeds up a
            # varied watchlist without hammering any one shop.
            self.log(f"checking {len(products)} products, {workers} at a time")
            ordered: dict[int, CheckResult] = {}
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(self.check, p, use_llm): i
                    for i, p in enumerate(products)
                }
                for future in as_completed(futures):
                    index = futures[future]
                    try:
                        res = future.result()
                    except Exception as exc:  # noqa: BLE001 - one bad page must
                        # not take down the whole run
                        res = self._record_failure(
                            products[index], f"{type(exc).__name__}: {exc}"
                        )
                    ordered[index] = res
                    if on_progress:
                        on_progress(res)
            results = [ordered[i] for i in sorted(ordered)]
            if self.verbose:
                for res in results:
                    for line in res.log:
                        print(line)

        alerts = [a for res in results for a in res.alerts]
        from . import notify

        notify.send(self._dedupe_groups(alerts, results), self.cfg["notify"])
        return results

    def _dedupe_groups(
        self, alerts: list[Alert], results: list[CheckResult]
    ) -> list[Alert]:
        """One product watched at five shops should send one notification.

        When several shops in the same group fire the same kind of alert, only
        the cheapest one is worth waking you up for - and it says how many other
        shops it beat.
        """
        by_name = {r.product.name: r.product for r in results}
        keep: list[Alert] = []
        best: dict[tuple[str, str], tuple[Alert, float, int]] = {}

        for alert in alerts:
            product = by_name.get(alert.product)
            group = product.group if product else None
            if not group or alert.price is None:
                keep.append(alert)
                continue
            key = (group, alert.kind)
            price = alert.price
            current = best.get(key)
            if current is None:
                best[key] = (alert, price, 1)
            elif price < current[1]:
                best[key] = (alert, price, current[2] + 1)
            else:
                best[key] = (current[0], current[1], current[2] + 1)

        for alert, _price, count in best.values():
            if count > 1:
                product = by_name.get(alert.product)
                shop = ""
                if product:
                    rule = sites.match(product.url)
                    shop = rule.name if rule else urlparse(product.url).netloc
                alert.message += f" — cheapest of {count} shops" + (
                    f", at {shop}" if shop else ""
                )
            keep.append(alert)
        return keep
