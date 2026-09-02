"""The agent loop: fetch, extract, decide, remember, alert."""

from __future__ import annotations

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
        self.session = requests.Session()

    def log(self, msg: str) -> None:
        if self.verbose:
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
                            f"(target {format_price(p.target_price, cur)})",
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
                            f"{format_price(p.last_price, cur)} → {now}",
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
                        message=f"{label} is back in stock at {now}",
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
        self.log(f"→ {p.name}")
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
        return CheckResult(product=p, extraction=ex, alerts=alerts)

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
            product=p, extraction=Extraction(method="error"), alerts=alerts, error=error
        )

    # ------------------------------------------------------------------
    def check_all(
        self, names: list[str] | None = None, use_llm: bool = True
    ) -> list[CheckResult]:
        products = (
            [self.store.get_product(n) for n in names]
            if names
            else self.store.list_products(active_only=True)
        )
        products = [p for p in products if p]

        results, alerts = [], []
        for p in products:
            res = self.check(p, use_llm=use_llm)
            results.append(res)
            alerts.extend(res.alerts)

        from . import notify

        notify.send(alerts, self.cfg["notify"])
        return results
