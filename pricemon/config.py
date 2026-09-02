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
        "respect_robots": True,
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
    },
    "notify": {
        "console": True,
        "desktop": True,  # macOS notification centre
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
