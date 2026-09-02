"""Command line interface: pricemon <command>."""

from __future__ import annotations

import argparse
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config as config_mod
from . import cron as cron_mod
from . import notify
from .agent import Agent
from .models import Extraction, Product, utcnow
from .money import format_price
from .sites import canonical_url as sites_canonical
from .storage import Store

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"


def _now_local() -> datetime:
    """Local wall-clock time, timezone-aware."""
    return datetime.now(timezone.utc).astimezone()


def _open(args) -> tuple[Store, dict]:
    config_mod.ensure_home()
    cfg = config_mod.load()
    if getattr(args, "model", None):
        cfg["llm"]["model"] = args.model
    return Store(config_mod.db_path()), cfg


def _slug(url: str, title: str | None) -> str:
    import re
    from urllib.parse import urlparse

    base = (
        title or urlparse(url).path.rstrip("/").split("/")[-1] or urlparse(url).netloc
    )
    slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")
    return slug[:48] or "product"


# --------------------------------------------------------------------------
def cmd_add(args) -> int:
    store, cfg = _open(args)
    agent = Agent(store, cfg, verbose=not args.quiet)

    url = args.url
    if args.search:
        from .llm import LLM
        from .search import rank_with_ai, search

        print(f"searching for {args.search!r} ...")
        results = search(args.search, cfg, retailers=args.retailer or None, limit=6)
        if not args.no_llm:
            results = rank_with_ai(args.search, results, LLM(cfg["llm"]))
        priced = [r for r in results if r.price is not None]
        if not priced:
            print(
                f"no product page with a readable price was found for {args.search!r}",
                file=sys.stderr,
            )
            for r in results[:5]:
                print(f"  tried {r.url}  ({r.note})", file=sys.stderr)
            print(
                "\ntry naming the exact model, narrowing with "
                "--retailer amazon, or pasting the product link directly",
                file=sys.stderr,
            )
            return 1
        if args.pick is None and len(priced) > 1:
            print(f"\n{BOLD}  #  PRICE        RETAILER         PRODUCT{RESET}")
            for i, r in enumerate(priced, 1):
                print(f"{i:>3}  {r.describe()}")
            print(
                "\nre-run with --pick N to track one of these "
                "(or --pick 1 to take the best match)"
            )
            return 0
        chosen = priced[(args.pick or 1) - 1]
        url = chosen.url
        args.name = args.name or None
        print(f"picked: {chosen.describe()}")

    if not url:
        print("error: give a product URL, or --search 'product name'", file=sys.stderr)
        return 1
    url = sites_canonical(url)
    args.url = url

    print(f"Fetching {url} ...")
    try:
        ex, candidates = agent.scrape(args.url, use_llm=not args.no_llm)
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        if not args.force:
            return 1
        ex, candidates = None, []

    name = args.name or _slug(args.url, ex.title if ex else None)
    if store.get_product(name):
        print(
            f"error: a product named {name!r} already exists "
            f"(pass --name to choose another)",
            file=sys.stderr,
        )
        return 1

    if ex and ex.ok:
        print(
            f"  found {format_price(ex.price, ex.currency)} via {ex.method} "
            f"(confidence {ex.confidence:.2f})"
        )
        if ex.title:
            print(f"  title: {ex.title}")
        if len(candidates) > 1 and args.verbose:
            print("  other candidates:")
            for c in candidates[1:5]:
                print(
                    f"    {c.method:16} {format_price(c.price, c.currency):>12}  {c.selector or ''}"
                )
    elif not args.force:
        print(
            "error: no price found. Re-run with --selector 'css.selector' or --force.",
            file=sys.stderr,
        )
        return 1

    p = Product(
        name=name,
        url=args.url,
        title=(ex.title if ex else None),
        image=(ex.image if ex else None),
        selector=args.selector,
        learned_selector=(
            ex.selector if ex and ex.method in ("llm", "heuristic") else None
        ),
        target_price=args.target,
        currency=(ex.currency if ex else None),
        notes=args.notes or "",
        last_price=(ex.price if ex and ex.ok else None),
        last_in_stock=(ex.in_stock if ex else None),
        last_checked=(utcnow() if ex and ex.ok else None),
    )
    store.add_product(p)
    if ex and ex.ok:
        store.record(p, ex)
    target = f", target {format_price(args.target, p.currency)}" if args.target else ""
    print(f"{BOLD}watching{RESET} {name}{target}")
    return 0


def cmd_search(args) -> int:
    from .search import rank_with_ai, search

    store, cfg = _open(args)
    store.close()
    print(f"searching for {args.query!r} ...")
    results = search(args.query, cfg, retailers=args.retailer or None, limit=args.limit)
    if not results:
        print("no product pages found - try more specific words, or a brand name")
        return 1

    if not args.no_llm:
        from .llm import LLM

        results = rank_with_ai(args.query, results, LLM(cfg["llm"]))

    print(f"\n{BOLD}  #  PRICE        RETAILER         PRODUCT{RESET}")
    for i, r in enumerate(results, 1):
        print(f"{i:>3}  {r.describe()}")
        if r.note:
            print(f"     {DIM}{r.note[:88]}{RESET}")
    print(
        f"\ntrack one with:  pricemon add --search {args.query!r} --pick 1 --target 199"
    )
    return 0


def _slugify(text: str, limit: int = 40) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit] or "product"


def cmd_compare(args) -> int:
    """Track one product at every shop that sells it, and watch the cheapest."""
    from .search import SearchBlocked, rank_with_ai, search

    store, cfg = _open(args)

    print(f"finding shops selling {args.query!r} ...")
    try:
        results = search(
            args.query, cfg, retailers=args.retailer or None, limit=args.shops * 2
        )
    except SearchBlocked as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not args.no_llm:
        from .llm import LLM

        results = rank_with_ai(args.query, results, LLM(cfg["llm"]))

    priced = [r for r in results if r.price is not None][: args.shops]
    if not priced:
        print("no shop with a readable price was found", file=sys.stderr)
        for r in results[:5]:
            print(f"  tried {r.url}  ({r.note})", file=sys.stderr)
        return 1

    group = args.name or _slugify(args.query)
    existing = {p.url for p in store.group_members(group)}
    added, skipped = [], []

    for result in priced:
        if result.url in existing:
            skipped.append(result)
            continue
        name = f"{group}@{_slugify(result.retailer, 20)}"
        n, suffix = name, 2
        while store.get_product(name):
            name = f"{n}-{suffix}"
            suffix += 1
        product = Product(
            name=name,
            url=result.url,
            title=result.title,
            group=group,
            target_price=args.target,
            currency=result.currency,
            last_price=result.price,
            last_in_stock=result.in_stock,
            last_checked=utcnow(),
        )
        store.add_product(product)
        store.record(
            product,
            Extraction(
                price=result.price,
                currency=result.currency,
                in_stock=result.in_stock,
                title=result.title,
                method="search",
            ),
        )
        added.append((product, result))

    print(f"\n{BOLD}tracking {len(added)} shop(s) as group {group!r}{RESET}")
    for product, result in added:
        print(f"  {result.describe()}")
    for result in skipped:
        print(f"  {DIM}already tracked: {result.retailer}{RESET}")

    cheapest = min(priced, key=lambda r: r.price or 1e15)
    print(
        f"\ncheapest right now: {format_price(cheapest.price, cheapest.currency)} "
        f"at {cheapest.retailer}"
    )
    if args.target:
        print(
            f"you will be told when any of them reaches "
            f"{format_price(args.target, cheapest.currency)}"
        )
    print(f"\nsee them together with:  pricemon group {group}")
    return 0


def cmd_group(args) -> int:
    store, _cfg = _open(args)
    if not args.name:
        groups = store.groups()
        if not groups:
            print('no groups yet — make one with:  pricemon compare "product name"')
            return 0
        for group in groups:
            members = store.group_members(group)
            priced = [m for m in members if m.last_price is not None]
            best = min(priced, key=lambda m: m.last_price or 1e15) if priced else None
            best_txt = (
                f"{format_price(best.last_price, best.currency)} at {_retailer(best)}"
                if best
                else "no prices yet"
            )
            print(f"{group:<28} {len(members)} shops · cheapest {best_txt}")
        return 0

    members = store.group_members(args.name)
    if not members:
        print(f"no group named {args.name!r}", file=sys.stderr)
        return 1

    priced = sorted(
        (m for m in members if m.last_price is not None),
        key=lambda m: m.last_price or 0,
    )
    unpriced = [m for m in members if m.last_price is None]
    title = next((m.title for m in members if m.title), args.name)
    print(f"{BOLD}{title[:70]}{RESET}")
    print(f"{DIM}{len(members)} shops · group {args.name}{RESET}\n")

    for i, m in enumerate(priced):
        stats = store.price_stats(m)
        low = (
            f" · low {format_price(stats['lo'], m.currency)}"
            if stats and stats["n"]
            else ""
        )
        stock = (
            ""
            if m.last_in_stock is None
            else ("" if m.last_in_stock else " · OUT OF STOCK")
        )
        mark = "→" if i == 0 else " "
        target = (
            f" · target {format_price(m.target_price, m.currency)}"
            if m.target_price
            else ""
        )
        print(
            f" {mark} {format_price(m.last_price, m.currency):>12}  "
            f"{_retailer(m):<16}{DIM}{low}{stock}{target}{RESET}"
        )
    for m in unpriced:
        print(f"   {'—':>12}  {_retailer(m):<16}{DIM} no price read{RESET}")

    if priced:
        best = priced[0]
        spread = (priced[-1].last_price or 0) - (best.last_price or 0)
        if spread > 0:
            print(
                f"\ncheapest is {_retailer(best)} — "
                f"{format_price(spread, best.currency)} less than the dearest"
            )
    return 0


def _retailer(product: Product) -> str:
    from urllib.parse import urlparse

    from . import sites

    rule = sites.match(product.url)
    return rule.name if rule else urlparse(product.url).netloc.replace("www.", "")


def cmd_list(args) -> int:
    store, _cfg = _open(args)
    products = store.list_products()
    if not products:
        print("nothing watched yet - add one with:  pricemon add <url> --target 99")
        return 0

    print(
        f"{BOLD}{'NAME':<28} {'PRICE':>12} {'TARGET':>10}  {'STOCK':<6} {'CHECKED':<17}{RESET}"
    )
    for p in products:
        stats = store.price_stats(p)
        lo = (
            f" low {format_price(stats['lo'], p.currency)}"
            if stats and stats["n"]
            else ""
        )
        stock = "-" if p.last_in_stock is None else ("yes" if p.last_in_stock else "NO")
        checked = (p.last_checked or "never")[:16].replace("T", " ")
        flag = "" if p.active else " (paused)"
        hit = (
            p.target_price is not None
            and p.last_price is not None
            and p.last_price <= p.target_price
        )
        mark = " 🎯" if hit else ""
        print(
            f"{p.name[:28]:<28} {format_price(p.last_price, p.currency):>12} "
            f"{format_price(p.target_price, p.currency) if p.target_price else '-':>10}  "
            f"{stock:<6} {checked:<17}{DIM}{lo}{flag}{RESET}{mark}"
        )
    return 0


def cmd_check(args) -> int:
    store, cfg = _open(args)
    if args.quiet:
        # cron mode: one timestamped line per alert, printed below - the
        # console notifier would otherwise duplicate every message.
        cfg["notify"] = {**cfg["notify"], "console": False}
    agent = Agent(store, cfg, verbose=not args.quiet)
    if not args.quiet:
        print(f"{DIM}LLM fallback: {agent.llm.describe()}{RESET}")

    results = agent.check_all(names=args.names or None, use_llm=not args.no_llm)
    if not results:
        print("nothing to check")
        return 0

    alerts = [a for r in results for a in r.alerts]
    failed = [r for r in results if r.error]
    if args.quiet and alerts:
        # cron mode: stay silent unless something actually happened
        for a in alerts:
            print(f"{_now_local():%Y-%m-%d %H:%M} {a.kind}: {a.message}")
    if not args.quiet:
        print(
            f"\n{len(results)} checked · {len(alerts)} alert(s) · {len(failed)} failed"
        )
    return 1 if failed and len(failed) == len(results) else 0


def cmd_watch(args) -> int:
    store, cfg = _open(args)
    agent = Agent(store, cfg, verbose=True)
    print(f"watching every {args.interval}s - ctrl-c to stop")
    try:
        while True:
            print(f"\n{BOLD}=== {_now_local():%Y-%m-%d %H:%M:%S} ==={RESET}")
            agent.check_all(use_llm=not args.no_llm)
            jitter = random.uniform(0, args.interval * 0.1)
            time.sleep(args.interval + jitter)
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


def cmd_history(args) -> int:
    store, _cfg = _open(args)
    p = store.get_product(args.name)
    if not p:
        print(f"no product named {args.name!r}", file=sys.stderr)
        return 1
    rows = store.history(p, limit=args.limit)
    if not rows:
        print("no observations yet")
        return 0

    prices = [r["price"] for r in rows if r["price"] is not None]
    lo, hi = (min(prices), max(prices)) if prices else (0, 0)
    print(f"{BOLD}{p.name}{RESET}  {p.url}")
    for r in rows:
        ts = r["ts"][:16].replace("T", " ")
        if r["price"] is None:
            print(f"{ts}  {'—':>12}  {DIM}{(r['error'] or 'no price')[:60]}{RESET}")
            continue
        width = 28
        pos = 0 if hi == lo else int((r["price"] - lo) / (hi - lo) * (width - 1))
        bar = " " * pos + "●" + " " * (width - pos - 1)
        stock = "" if r["in_stock"] is None else ("" if r["in_stock"] else " OUT")
        print(
            f"{ts}  {format_price(r['price'], r['currency'] or p.currency):>12}  "
            f"{DIM}|{bar}|{RESET} {r['method']}{stock}"
        )
    if prices:
        print(
            f"\nlow {format_price(lo, p.currency)} · high {format_price(hi, p.currency)} "
            f"· avg {format_price(sum(prices) / len(prices), p.currency)} · {len(prices)} points"
        )
    return 0


def cmd_alerts(args) -> int:
    store, _cfg = _open(args)
    rows = store.recent_alerts(limit=args.limit)
    if not rows:
        print("no alerts recorded yet")
        return 0
    for r in rows:
        print(
            f"{r['ts'][:16].replace('T', ' ')}  {notify.ICONS.get(r['kind'], '•')} {r['message']}"
        )
    return 0


def cmd_remove(args) -> int:
    store, _cfg = _open(args)
    print(
        f"removed {args.name}"
        if store.remove_product(args.name)
        else f"no product named {args.name!r}"
    )
    return 0


def cmd_set(args) -> int:
    store, _cfg = _open(args)
    p = store.get_product(args.name)
    if not p:
        print(f"no product named {args.name!r}", file=sys.stderr)
        return 1
    if args.target is not None:
        p.target_price = None if args.target < 0 else args.target
    if args.selector is not None:
        p.selector = args.selector or None
        p.learned_selector = None
    if args.url:
        p.url = args.url
    if args.pause:
        p.active = False
    if args.resume:
        p.active = True
    store.update_product(p)
    print(
        f"updated {p.name}: target={format_price(p.target_price, p.currency)} "
        f"selector={p.selector or p.learned_selector or 'auto'} "
        f"{'active' if p.active else 'paused'}"
    )
    return 0


def cmd_config(args) -> int:
    config_mod.ensure_home()
    cfg = config_mod.load()
    from .llm import LLM

    print(f"config : {config_mod.config_path()}")
    print(f"database: {config_mod.db_path()}")
    print(f"llm     : {LLM(cfg['llm']).describe()}")
    print(
        f"alerts  : drop >= {cfg['alerts']['drop_pct']}% · "
        f"desktop={cfg['notify']['desktop']} webhook={bool(cfg['notify']['webhook_url'])} "
        f"email={bool(cfg['notify']['email'])}"
    )
    return 0


def cmd_test_notify(args) -> int:
    config_mod.ensure_home()
    notify.test(config_mod.load()["notify"])
    print("sent a test alert through every configured channel")
    return 0


def cmd_install_cron(args) -> int:
    from . import schedule

    config_mod.ensure_home()
    times = cron_mod.parse_times(args.times)
    kind = "scheduled tasks" if schedule.is_windows() else "cron entries"

    if args.dry_run:
        print(schedule.preview(times))
        return 0
    print(f"{BOLD}installed{RESET} {len(times)} {kind}:")
    print(schedule.install(times))
    print(f"\nlog: {schedule.log_file()}")
    print(f"verify with:  {schedule.verify_hint()}")
    return 0


def cmd_uninstall_cron(args) -> int:
    from . import schedule

    kind = "scheduled tasks" if schedule.is_windows() else "cron entries"
    print(
        f"removed pricemon {kind}"
        if schedule.uninstall()
        else f"no pricemon {kind} found"
    )
    return 0


def cmd_serve(args) -> int:
    from .webapp import serve

    serve(port=args.port, host=args.host, open_browser=not args.no_open)
    return 0


def cmd_app(args) -> int:
    """Open the tracker as a desktop window."""
    from . import desktop

    return desktop.launch(port=args.port)


def cmd_install_desktop(args) -> int:
    from . import desktop

    path = desktop.install_launcher()
    print(f"installed application launcher: {path}")
    print("it should now appear in your applications menu as 'Price Monitor'")
    return 0


def cmd_report(args) -> int:
    from .report import build_report

    store, _cfg = _open(args)
    out = Path(args.out).expanduser() if args.out else config_mod.home() / "report.html"
    build_report(store, out)
    print(f"wrote {out}")
    return 0


def cmd_export(args) -> int:
    import csv

    store, _cfg = _open(args)
    out = Path(args.out).expanduser()
    with out.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            ["product", "url", "ts", "price", "currency", "in_stock", "method", "error"]
        )
        for p in store.list_products():
            for r in store.history(p, limit=100000):
                w.writerow(
                    [
                        p.name,
                        p.url,
                        r["ts"],
                        r["price"],
                        r["currency"],
                        r["in_stock"],
                        r["method"],
                        r["error"],
                    ]
                )
    print(f"wrote {out}")
    return 0


# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="pricemon",
        description="An AI agent that watches product pages and tells you when the price drops.",
    )
    ap.add_argument("--model", help="override the LLM model for this run (e.g. haiku)")
    # Same flag accepted after the subcommand too; SUPPRESS keeps the outer value
    # when the inner one is omitted.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--model",
        default=argparse.SUPPRESS,
        help="override the LLM model for this run (e.g. haiku)",
    )
    sub = ap.add_subparsers(dest="command", required=True)

    a = sub.add_parser("add", parents=[common], help="start watching a product page")
    a.add_argument("url", nargs="?", help="product URL (omit when using --search)")
    a.add_argument("--search", help="find the product by name instead of a URL")
    a.add_argument("--pick", type=int, help="with --search: track result number N")
    a.add_argument(
        "--retailer",
        action="append",
        help="limit --search to a retailer (repeatable), e.g. --retailer amazon",
    )
    a.add_argument("--name", help="short name (default: derived from the page title)")
    a.add_argument(
        "--target", type=float, help="alert when the price reaches this or lower"
    )
    a.add_argument(
        "--selector",
        help="pin a CSS selector, e.g. 'span.price' or 'meta[itemprop=price]@content'",
    )
    a.add_argument("--notes", default="")
    a.add_argument(
        "--no-llm", action="store_true", help="deterministic extraction only"
    )
    a.add_argument(
        "--force", action="store_true", help="add even if no price was found"
    )
    a.add_argument("--verbose", "-v", action="store_true")
    a.add_argument("--quiet", "-q", action="store_true")
    a.set_defaults(func=cmd_add)

    a = sub.add_parser(
        "search", parents=[common], help="find product pages by name, with prices"
    )
    a.add_argument("query")
    a.add_argument("--limit", type=int, default=6)
    a.add_argument(
        "--retailer",
        action="append",
        help="limit to a retailer (repeatable), e.g. --retailer walmart",
    )
    a.add_argument("--no-llm", action="store_true", help="skip AI re-ranking")
    a.set_defaults(func=cmd_search)

    a = sub.add_parser(
        "compare",
        parents=[common],
        help="track one product at every shop that sells it, and watch the cheapest",
    )
    a.add_argument("query", help='product name, e.g. "sony wh-1000xm5"')
    a.add_argument("--shops", type=int, default=6, help="how many shops to track")
    a.add_argument("--target", type=float, help="alert when any shop reaches this")
    a.add_argument("--name", help="group name (default: from the query)")
    a.add_argument(
        "--retailer", action="append", help="limit to a retailer (repeatable)"
    )
    a.add_argument("--no-llm", action="store_true")
    a.set_defaults(func=cmd_compare)

    a = sub.add_parser("group", help="compare the shops tracking one product")
    a.add_argument("name", nargs="?", help="group name (omit to list all groups)")
    a.set_defaults(func=cmd_group)

    a = sub.add_parser("list", help="show everything being watched")
    a.set_defaults(func=cmd_list)

    a = sub.add_parser(
        "check", parents=[common], help="check prices now and fire alerts"
    )
    a.add_argument("names", nargs="*", help="limit to these products")
    a.add_argument("--no-llm", action="store_true")
    a.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="only print alerts (use this in cron)",
    )
    a.set_defaults(func=cmd_check)

    a = sub.add_parser(
        "watch", parents=[common], help="keep checking on an interval in the foreground"
    )
    a.add_argument(
        "--interval",
        type=int,
        default=3600,
        help="seconds between rounds (default 3600)",
    )
    a.add_argument("--no-llm", action="store_true")
    a.set_defaults(func=cmd_watch)

    a = sub.add_parser("history", help="price history for one product")
    a.add_argument("name")
    a.add_argument("--limit", type=int, default=40)
    a.set_defaults(func=cmd_history)

    a = sub.add_parser("alerts", help="recent alerts")
    a.add_argument("--limit", type=int, default=20)
    a.set_defaults(func=cmd_alerts)

    a = sub.add_parser("remove", help="stop watching a product")
    a.add_argument("name")
    a.set_defaults(func=cmd_remove)

    a = sub.add_parser("set", help="change target price, selector, url or paused state")
    a.add_argument("name")
    a.add_argument("--target", type=float, help="new target (negative clears it)")
    a.add_argument("--selector", help="pin a CSS selector ('' to clear)")
    a.add_argument("--url")
    a.add_argument("--pause", action="store_true")
    a.add_argument("--resume", action="store_true")
    a.set_defaults(func=cmd_set)

    a = sub.add_parser(
        "config", help="show where things live and how the agent is wired"
    )
    a.set_defaults(func=cmd_config)

    a = sub.add_parser("test-notify", help="send a test alert through every channel")
    a.set_defaults(func=cmd_test_notify)

    a = sub.add_parser(
        "install-cron",
        help="run the agent automatically twice a day (cron, or Windows Task "
        "Scheduler) — default 08:00 and 20:00",
    )
    a.add_argument("--times", default="08:00,20:00", help="comma-separated HH:MM")
    a.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be scheduled, change nothing",
    )
    a.set_defaults(func=cmd_install_cron)

    a = sub.add_parser("uninstall-cron", help="remove the scheduled runs")
    a.set_defaults(func=cmd_uninstall_cron)

    a = sub.add_parser("app", help="open the tracker as a desktop window")
    a.add_argument("--port", type=int, default=8787)
    a.set_defaults(func=cmd_app)

    a = sub.add_parser("serve", help="run the web UI without opening a window")
    a.add_argument("--port", type=int, default=8787)
    a.add_argument("--host", default="127.0.0.1")
    a.add_argument("--no-open", action="store_true")
    a.set_defaults(func=cmd_serve)

    a = sub.add_parser(
        "install-desktop",
        help="add Price Monitor to your applications menu (Linux / Windows)",
    )
    a.set_defaults(func=cmd_install_desktop)

    a = sub.add_parser("report", help="write an HTML dashboard of price history")
    a.add_argument("--out")
    a.set_defaults(func=cmd_report)

    a = sub.add_parser("export", help="export all observations to CSV")
    a.add_argument("out")
    a.set_defaults(func=cmd_export)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
