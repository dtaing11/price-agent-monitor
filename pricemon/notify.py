"""Getting the alert in front of you: terminal, desktop, webhook, email.

Desktop notifications work on Linux (notify-send) and macOS (osascript).
"""

from __future__ import annotations

import json
import shutil
import smtplib
import subprocess
import sys
from email.message import EmailMessage
from typing import List

import requests

from .models import Alert

ICONS = {
    "target_hit": "🎯", "price_drop": "📉", "price_rise": "📈",
    "back_in_stock": "📦", "out_of_stock": "🚫", "error": "⚠️",
}
COLORS = {
    "target_hit": "\033[1;32m", "price_drop": "\033[32m", "price_rise": "\033[33m",
    "back_in_stock": "\033[1;36m", "out_of_stock": "\033[35m", "error": "\033[31m",
}
RESET = "\033[0m"


def _console(alerts: List[Alert]) -> None:
    tty = sys.stdout.isatty()
    for a in alerts:
        icon = ICONS.get(a.kind, "•")
        if tty:
            print(f"{COLORS.get(a.kind, '')}{icon} {a.message}{RESET}")
        else:
            print(f"{icon} {a.message}")


def _desktop(alerts: List[Alert]) -> None:
    """notify-send on Linux, osascript on macOS.  Silently skipped elsewhere."""
    if not alerts:
        return
    title = "Price Monitor"
    body = "\n".join(f"{ICONS.get(a.kind, '•')} {a.message}" for a in alerts[:5])

    if shutil.which("notify-send"):                      # Linux
        urgency = "critical" if any(a.kind == "target_hit" for a in alerts) else "normal"
        subprocess.run(["notify-send", "-u", urgency, "-a", "pricemon", title, body],
                       capture_output=True)
        return
    if sys.platform == "darwin" and shutil.which("osascript"):
        safe = body.replace("\\", "").replace('"', "'").replace("\n", " · ")
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{safe}" with title "{title}" sound name "Glass"'],
            capture_output=True)


def _webhook(url: str, alerts: List[Alert]) -> None:
    text = "\n".join(f"{ICONS.get(a.kind, '•')} {a.message}" for a in alerts)
    # "text" satisfies Slack; "content" satisfies Discord; "alerts" is for you.
    payload = {
        "text": text,
        "content": text,
        "alerts": [{"kind": a.kind, "product": a.product, "message": a.message,
                    "price": a.price} for a in alerts],
    }
    try:
        requests.post(url, json=payload, timeout=15)
    except requests.RequestException as exc:
        print(f"  webhook failed: {exc}", file=sys.stderr)


def _email(cfg: dict, alerts: List[Alert]) -> None:
    msg = EmailMessage()
    msg["Subject"] = f"Price alert: {alerts[0].message[:60]}"
    msg["From"] = cfg.get("from") or cfg["user"]
    msg["To"] = cfg["to"]
    msg.set_content("\n".join(a.message for a in alerts))
    try:
        with smtplib.SMTP(cfg["host"], int(cfg.get("port", 587)), timeout=30) as smtp:
            smtp.starttls()
            if cfg.get("user"):
                smtp.login(cfg["user"], cfg["password"])
            smtp.send_message(msg)
    except Exception as exc:                              # noqa: BLE001 - never crash a run
        print(f"  email failed: {exc}", file=sys.stderr)


def send(alerts: List[Alert], cfg: dict) -> None:
    if not alerts:
        return
    if cfg.get("console", True):
        _console(alerts)
    if cfg.get("desktop", True):
        _desktop(alerts)
    if cfg.get("webhook_url"):
        _webhook(cfg["webhook_url"], alerts)
    if cfg.get("email"):
        _email(cfg["email"], alerts)


def test(cfg: dict) -> None:
    send([Alert(kind="target_hit", product="Test Product",
                message="Test Product hit your target: $19.99 (target $25.00)",
                price=19.99)], cfg)
