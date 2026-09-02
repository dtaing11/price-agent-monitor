"""Settings, loaded from ~/.price-monitor/config.yaml (override with PRICEMON_HOME)."""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

DEFAULTS: dict[str, Any] = {
    "fetch": {
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 pricemon/0.1"
        ),
        "timeout": 25,
        "max_retries": 3,
        "min_delay_per_domain": 3.0,  # seconds between hits on the same host
        # How many sites to check at once. Requests to the same host still
        # queue behind min_delay_per_domain, so this speeds up a varied
        # watchlist without hitting any one shop harder.
        "workers": 6,
        # Browser renders and LLM calls are much heavier than a plain fetch;
        # this many may run at once, regardless of `workers`.
        "heavy_workers": 2,
        # Off by default: this is a personal watchlist checked a couple of
        # times a day, not a crawler. Politeness is enforced by the throttle
        # and retry budget above instead. Set true to honour robots.txt.
        "respect_robots": False,
        # auto: render with a headless browser only for sites that need it
        # (Amazon, Target, ...) or when plain HTTP finds no price.
        # never | auto | always. Needs playwright installed.
        "browser": "auto",
    },
    "llm": {
        # auto -> claude CLI (your OAuth login) if present, else the API key, else off
        "backend": "auto",  # auto | claude_cli | api | off
        "model": "claude-opus-5",  # CLI accepts aliases too: opus | sonnet | haiku
        "max_html_chars": 40000,
        "timeout": 180,
    },
    "alerts": {
        "drop_pct": 5.0,  # notify when price falls this much vs last check
        "notify_price_rise": False,
        "notify_stock_change": True,
        "fail_streak_alert": 3,  # notify after N consecutive scrape failures
        # A change larger than this is re-read and confirmed before it alerts:
        # a huge overnight "drop" is usually a redesigned page, not a sale.
        # 0 disables the check.
        "implausible_pct": 65.0,
    },
    "notify": {
        "console": True,
        "desktop": True,  # macOS notification centre
        # Phone push. ntfy: pick a topic, subscribe in the ntfy app, put the
        # same name here. Anyone who knows the topic can publish to it, so
        # choose something unguessable.
        "ntfy": {"topic": None, "server": "https://ntfy.sh", "token": None},
        # Telegram: create a bot with @BotFather, message it once, then read
        # your chat id from api.telegram.org/bot<token>/getUpdates
        "telegram": {"bot_token": None, "chat_id": None},
        "webhook_url": None,  # Slack / Discord / any JSON endpoint
        "email": None,  # {host, port, user, password, to, from}
    },
}


def home() -> Path:
    return Path(
        os.environ.get("PRICEMON_HOME", Path.home() / ".price-monitor")
    ).expanduser()


def config_path() -> Path:
    return home() / "config.yaml"


def db_path() -> Path:
    return home() / "prices.db"


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = (
            _merge(out[k], v)
            if isinstance(v, dict) and isinstance(out.get(k), dict)
            else v
        )
    return out


def load() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        return copy.deepcopy(DEFAULTS)
    with path.open() as fh:
        return _merge(DEFAULTS, yaml.safe_load(fh) or {})


def ensure_home() -> Path:
    h = home()
    h.mkdir(parents=True, exist_ok=True)
    if not config_path().exists():
        with config_path().open("w") as fh:
            yaml.safe_dump(DEFAULTS, fh, sort_keys=False, allow_unicode=True)
    return h
