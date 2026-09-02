"""The desktop app's backend: a local HTTP server with a small JSON API.

Deliberately stdlib-only - the app has to start on a fresh machine with nothing
but `pip install -r requirements.txt`. Checks run on a background worker so the
UI stays responsive while pages are being fetched.
"""

from __future__ import annotations

import json
import mimetypes
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from . import config as config_mod
from . import sites
from .agent import Agent
from .models import Product
from .storage import Store

STATIC = Path(__file__).parent / "static"


class JobRunner:
    """Runs price checks off the request thread and reports progress."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.busy = False
        self.total = 0
        self.done = 0
        self.current = ""
        self.last_finished: str | None = None
        self.last_summary: str = ""
        self.log: list[str] = []

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "busy": self.busy,
                "total": self.total,
                "done": self.done,
                "current": self.current,
                "last_finished": self.last_finished,
                "last_summary": self.last_summary,
                "log": self.log[-40:],
            }

    def start(self, names: list[str] | None, cfg: dict) -> bool:
        with self.lock:
            if self.busy:
                return False
            self.busy = True
            self.done = 0
            self.current = ""
            self.log = []
        threading.Thread(target=self._run, args=(names, cfg), daemon=True).start()
        return True

    def _run(self, names: list[str] | None, cfg: dict) -> None:
        store = Store(config_mod.db_path())
        try:
            agent = Agent(store, cfg, verbose=False)
            products = (
                [p for p in (store.get_product(n) for n in names) if p]
                if names
                else store.list_products(active_only=True)
            )
            with self.lock:
                self.total = len(products)
                self.current = f"{len(products)} sites at once"

            def progress(result) -> None:
                with self.lock:
                    self.done += 1
                    label = result.product.title or result.product.name
                    if result.error:
                        self.log.append(f"✗ {label[:48]}: {result.error[:70]}")
                    else:
                        self.log.append(
                            f"✓ {label[:48]}: {result.extraction.price} "
                            f"({result.extraction.method})"
                        )
                    remaining = self.total - self.done
                    self.current = f"{remaining} to go" if remaining else "finishing up"

            # check_all fans out across sites and sends the notifications.
            results = agent.check_all(
                names=[p.name for p in products], on_progress=progress
            )
            alerts = [a for r in results for a in r.alerts]
            with self.lock:
                self.last_summary = f"{len(products)} checked, {len(alerts)} alert(s)"
        except Exception as exc:  # noqa: BLE001 - a failed run must not kill the app
            with self.lock:
                self.log.append(f"✗ run failed: {exc}")
                self.last_summary = f"run failed: {exc}"
        finally:
            store.close()
            with self.lock:
                self.busy = False
                self.current = ""
                self.last_finished = datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                )


JOBS = JobRunner()


def _product_payload(store: Store, product: Product, history_limit: int = 400) -> dict:
    rows = store.history(product, limit=history_limit)
    stats = store.price_stats(product)
    points = [
        {
            "ts": r["ts"],
            "price": r["price"],
            "in_stock": r["in_stock"],
            "method": r["method"],
            "error": r["error"],
        }
        for r in rows
    ]
    prices = [p["price"] for p in points if p["price"] is not None]
    rule = sites.match(product.url)
    first, last = (prices[0], prices[-1]) if prices else (None, None)
    return {
        "name": product.name,
        "title": product.title or product.name,
        "image": product.image,
        "group": product.group,
        "url": product.url,
        "selector": product.selector,
        "learned_selector": product.learned_selector,
        "target_price": product.target_price,
        "currency": product.currency,
        "active": product.active,
        "notes": product.notes,
        "last_price": product.last_price,
        "last_in_stock": product.last_in_stock,
        "last_checked": product.last_checked,
        "fail_count": product.fail_count,
        "retailer": rule.name if rule else urlparse(product.url).netloc,
        "low": stats["lo"] if stats else None,
        "high": stats["hi"] if stats else None,
        "avg": stats["avg"] if stats else None,
        "checks": stats["n"] if stats else 0,
        "change_pct": (
            None if not prices or not first else (last - first) / first * 100
        ),
        "target_hit": (
            product.target_price is not None
            and product.last_price is not None
            and product.last_price <= product.target_price
        ),
        "history": points,
    }


MASK = "••••••••"
SECRET_FIELDS = (
    ("ntfy", "token"),
    ("telegram", "bot_token"),
    ("email", "password"),
)


def _settings_payload() -> dict:
    """Current settings for the UI, with secrets masked.

    The app is served on localhost only, but there is no reason to hand a
    password back out just to render a form - the mask goes out, and a field
    still holding the mask on save means "leave it as it was".
    """
    cfg = config_mod.load()
    notify_cfg = json.loads(json.dumps(cfg["notify"]))  # deep copy
    for section, field in SECRET_FIELDS:
        holder = notify_cfg.get(section) or {}
        if isinstance(holder, dict) and holder.get(field):
            holder[field] = MASK
    return {
        "notify": notify_cfg,
        "alerts": cfg["alerts"],
        "fetch": {
            "workers": cfg["fetch"].get("workers"),
            "min_delay_per_domain": cfg["fetch"].get("min_delay_per_domain"),
            "browser": cfg["fetch"].get("browser"),
            "respect_robots": cfg["fetch"].get("respect_robots"),
        },
        "llm": {"backend": cfg["llm"].get("backend"), "model": cfg["llm"].get("model")},
        "home": str(config_mod.home()),
    }


def _apply_settings(body: dict) -> dict:
    """Merge what the form sent into config.yaml, keeping masked secrets."""
    cfg = config_mod.load()
    incoming = body or {}

    for group in ("notify", "alerts", "fetch", "llm"):
        section = incoming.get(group)
        if not isinstance(section, dict):
            continue
        for key, value in section.items():
            if isinstance(value, dict):
                current = cfg[group].get(key)
                cfg[group][key] = dict(current) if isinstance(current, dict) else {}
                for sub_key, sub_value in value.items():
                    if sub_value == MASK:
                        continue  # unchanged secret
                    cfg[group][key][sub_key] = _clean(sub_value)
            else:
                cfg[group][key] = _clean(value)

    config_mod.save(cfg)
    return _settings_payload()


def _clean(value):
    """Empty strings mean "not set", and numbers arrive from forms as strings."""
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return value


class Handler(BaseHTTPRequestHandler):
    server_version = "pricemon"

    # -- plumbing ---------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:
        return  # the terminal belongs to the user, not to access logs

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: Any, code: int = 200) -> None:
        self._send(code, json.dumps(data, default=str).encode(), "application/json")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            parsed = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _store(self) -> Store:
        return Store(config_mod.db_path())

    # -- routes -----------------------------------------------------------
    def do_GET(self) -> None:
        route = urlparse(self.path)
        path = route.path

        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path.startswith("/static/"):
            return self._static(path[len("/static/") :])

        if path == "/api/state":
            store = self._store()
            try:
                products = [_product_payload(store, p) for p in store.list_products()]
                alerts = [
                    {
                        "ts": r["ts"],
                        "kind": r["kind"],
                        "message": r["message"],
                        "price": r["price"],
                        "product": r["name"],
                    }
                    for r in store.recent_alerts(limit=60)
                ]
            finally:
                store.close()
            cfg = config_mod.load()
            from .llm import LLM

            return self._json(
                {
                    "products": products,
                    "alerts": alerts,
                    "job": JOBS.snapshot(),
                    "llm": LLM(cfg["llm"]).describe(),
                    "home": str(config_mod.home()),
                    "sites": [
                        {"name": n, "domain": d, "protection": p}
                        for n, d, p in sites.supported_sites()
                    ],
                }
            )

        if path == "/api/settings":
            return self._json(_settings_payload())

        if path == "/api/schedule":
            from . import schedule

            return self._json(schedule.status())

        if path == "/api/job":
            return self._json(JOBS.snapshot())

        if path == "/api/search":
            params = parse_query(route.query)
            query = (params.get("q") or "").strip()
            if len(query) < 2:
                return self._json({"error": "type a product name"}, 400)
            from .llm import LLM
            from .search import rank_with_ai, search

            cfg = config_mod.load()
            llm = LLM(cfg["llm"])
            plan: dict = {}

            def note_plan(products, reading):
                plan.update({"products": products, "reading": reading})

            try:
                results = search(
                    query,
                    cfg,
                    limit=int(params.get("limit") or 5),
                    llm=llm if params.get("ai", "1") != "0" else None,
                    on_plan=note_plan,
                )
                if params.get("ai", "1") != "0" and not plan:
                    results = rank_with_ai(query, results, llm)
            except Exception as exc:  # noqa: BLE001 - surface it in the UI
                return self._json({"error": f"search failed: {exc}"}, 502)
            from .search import normalize_query

            return self._json(
                {
                    "query": query,
                    "query_used": normalize_query(query),
                    "plan": plan,
                    "results": [
                        {
                            "title": r.title,
                            "url": r.url,
                            "retailer": r.retailer,
                            "price": r.price,
                            "currency": r.currency,
                            "in_stock": r.in_stock,
                            "note": r.note,
                            "method": r.method,
                            "matched": r.matched,
                        }
                        for r in results
                    ],
                }
            )

        return self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        route = urlparse(self.path)
        path = route.path
        body = self._body()

        if path == "/api/products/group":
            return self._add_group(body)

        if path == "/api/products":
            return self._add_product(body)

        if path == "/api/check":
            names = body.get("names")
            started = JOBS.start(names or None, config_mod.load())
            return self._json({"started": started, "job": JOBS.snapshot()})

        if path == "/api/settings":
            try:
                saved = _apply_settings(body)
            except (ValueError, KeyError) as exc:
                return self._json({"error": str(exc)}, 400)
            return self._json(saved)

        if path == "/api/schedule":
            from . import cron as cron_mod
            from . import schedule

            try:
                if body.get("enabled"):
                    times = cron_mod.parse_times(body.get("times") or "08:00,20:00")
                    schedule.install(times)
                else:
                    schedule.uninstall()
            except (ValueError, RuntimeError) as exc:
                return self._json({"error": str(exc)}, 400)
            return self._json(schedule.status())

        if path == "/api/test-notify":
            from . import notify

            notify.test({**config_mod.load()["notify"], "console": False})
            return self._json({"sent": True})

        if path == "/api/report":
            from .report import build_report

            store = self._store()
            try:
                out = config_mod.home() / "report.html"
                build_report(store, out)
            finally:
                store.close()
            return self._json({"path": str(out)})

        return self._json({"error": "not found"}, 404)

    def do_PATCH(self) -> None:
        route = urlparse(self.path)
        if not route.path.startswith("/api/products/"):
            return self._json({"error": "not found"}, 404)
        name = unquote(route.path.split("/api/products/", 1)[1])
        body = self._body()

        store = self._store()
        try:
            product = store.get_product(name)
            if not product:
                return self._json({"error": f"no product named {name}"}, 404)
            if "target_price" in body:
                raw = body["target_price"]
                product.target_price = None if raw in ("", None) else float(raw)
            if "selector" in body:
                product.selector = body["selector"] or None
                product.learned_selector = None
            if "active" in body:
                product.active = bool(body["active"])
            if "notes" in body:
                product.notes = str(body["notes"])[:500]
            store.update_product(product)
            payload = _product_payload(store, product)
        finally:
            store.close()
        return self._json(payload)

    def do_DELETE(self) -> None:
        route = urlparse(self.path)
        if not route.path.startswith("/api/products/"):
            return self._json({"error": "not found"}, 404)
        name = unquote(route.path.split("/api/products/", 1)[1])
        store = self._store()
        try:
            removed = store.remove_product(name)
        finally:
            store.close()
        return self._json({"removed": removed})

    # -- helpers ----------------------------------------------------------
    def _add_group(self, body: dict) -> None:
        """Track several pages together, as one thing to compare.

        Either the same product at several shops, or several products in the
        same category - the group holds both, and the app tells them apart by
        whether the members share a title.
        """
        urls = [u for u in (body.get("urls") or []) if isinstance(u, str)]
        if len(urls) < 2:
            return self._json({"error": "pick at least two to compare"}, 400)

        cfg = config_mod.load()
        store = self._store()
        added: list[dict] = []
        failed: list[dict] = []
        try:
            agent = Agent(store, cfg, verbose=False)
            group = _slug(body.get("name") or "") or None
            raw_target = body.get("target_price")
            target = None if raw_target in ("", None) else float(raw_target)

            for raw_url in urls[:12]:
                url = sites.canonical_url(raw_url)
                try:
                    extraction, _ = agent.scrape(url, use_llm=False)
                except Exception as exc:  # noqa: BLE001 - one bad shop is not fatal
                    failed.append({"url": url, "error": str(exc)[:160]})
                    continue

                group = group or _slug(extraction.title or _derive_name(url, None))
                rule = sites.match(url)
                retailer = (
                    rule.name if rule else urlparse(url).netloc.replace("www.", "")
                )
                name = f"{group}@{_slug(retailer, 20)}"
                base, suffix = name, 2
                while store.get_product(name):
                    name = f"{base}-{suffix}"
                    suffix += 1

                product = Product(
                    name=name,
                    url=url,
                    title=extraction.title,
                    image=extraction.image,
                    group=group,
                    target_price=target,
                    currency=extraction.currency,
                    last_price=extraction.price,
                    last_in_stock=extraction.in_stock,
                    last_checked=datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                )
                store.add_product(product)
                if extraction.ok:
                    store.record(product, extraction)
                added.append(_product_payload(store, product))
        finally:
            store.close()

        if not added:
            return self._json(
                {"error": "none of those pages gave a price", "failed": failed}, 422
            )
        return self._json({"group": group, "added": added, "failed": failed}, 201)

    def _add_product(self, body: dict) -> None:
        url = (body.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return self._json({"error": "a http(s) URL is required"}, 400)
        url = sites.canonical_url(url)

        cfg = config_mod.load()
        store = self._store()
        try:
            agent = Agent(store, cfg, verbose=False)
            try:
                extraction, _ = agent.scrape(url, use_llm=body.get("use_llm", True))
            except Exception as exc:  # noqa: BLE001 - report, don't crash the app
                return self._json({"error": str(exc)}, 502)

            name = (body.get("name") or "").strip() or _derive_name(
                url, extraction.title
            )
            if store.get_product(name):
                return self._json({"error": f"{name!r} is already tracked"}, 409)
            if not extraction.ok and not body.get("force"):
                return self._json(
                    {
                        "error": "no price found on that page",
                        "hint": "add a CSS selector, or tick 'add anyway'",
                        "title": extraction.title,
                    },
                    422,
                )

            target = body.get("target_price")
            product = Product(
                name=name,
                url=url,
                title=extraction.title,
                image=extraction.image,
                group=(body.get("group") or "").strip() or None,
                selector=(body.get("selector") or "").strip() or None,
                learned_selector=(
                    extraction.selector
                    if extraction.method in ("llm", "heuristic")
                    else None
                ),
                target_price=None if target in ("", None) else float(target),
                currency=extraction.currency,
                notes=(body.get("notes") or "").strip(),
                last_price=extraction.price,
                last_in_stock=extraction.in_stock,
                last_checked=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
            store.add_product(product)
            if extraction.ok:
                store.record(product, extraction)
            payload = _product_payload(store, product)
        finally:
            store.close()
        return self._json(payload, 201)

    def _static(self, rel: str) -> None:
        target = (STATIC / rel).resolve()
        if not str(target).startswith(str(STATIC.resolve())) or not target.is_file():
            return self._json({"error": "not found"}, 404)
        ctype, _ = mimetypes.guess_type(str(target))
        self._send(200, target.read_bytes(), ctype or "application/octet-stream")


def _slug(text: str, limit: int = 40) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:limit]


def _derive_name(url: str, title: str | None) -> str:
    import re

    base = (
        title or urlparse(url).path.rstrip("/").split("/")[-1] or urlparse(url).netloc
    )
    slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")
    return slug[:48] or "product"


def serve(
    port: int = 8787, host: str = "127.0.0.1", open_browser: bool = False
) -> None:
    config_mod.ensure_home()
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print(f"pricemon UI running at {url}  (ctrl-c to stop)")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()


def free_port(preferred: int = 8787) -> int:
    import socket

    for candidate in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", candidate))
                return int(sock.getsockname()[1])
            except OSError:
                continue
    return preferred


def parse_query(query: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(query).items()}
