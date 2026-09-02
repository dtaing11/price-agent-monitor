"""Getting the alert in front of you.

Channels: terminal, desktop (notify-send on Linux, osascript on macOS,
toast notifications on Windows), phone
(ntfy or Telegram), webhook (Slack/Discord), email.

A scheduled check runs at 08:00 and 20:00 whether or not you are at the
machine, so a desktop toast is easy to miss and a good price can be gone by the
evening. The phone channels exist for that reason: they arrive wherever you
are, with the product link attached so the alert is one tap from the page.
No channel is allowed to raise - a failed notification must never lose a run.
"""

from __future__ import annotations

import html
import os
import shutil
import smtplib
import subprocess
import sys
import unicodedata
from email.message import EmailMessage

import requests

from .models import Alert

ICONS = {
    "target_hit": "🎯",
    "price_drop": "📉",
    "price_rise": "📈",
    "back_in_stock": "📦",
    "out_of_stock": "🚫",
    "error": "⚠️",
    "needs_check": "🔍",
}
COLORS = {
    "target_hit": "\033[1;32m",
    "price_drop": "\033[32m",
    "price_rise": "\033[33m",
    "back_in_stock": "\033[1;36m",
    "out_of_stock": "\033[35m",
    "error": "\033[31m",
    "needs_check": "\033[33m",
}
RESET = "\033[0m"

# ntfy: 5 shouts through Do Not Disturb, 3 is a normal notification.
NTFY_PRIORITY = {
    "target_hit": 5,
    "back_in_stock": 4,
    "price_drop": 4,
    "out_of_stock": 3,
    "price_rise": 2,
    "error": 3,
    "needs_check": 3,
}
NTFY_TAGS = {
    "target_hit": "dart",
    "price_drop": "chart_with_downwards_trend",
    "price_rise": "chart_with_upwards_trend",
    "back_in_stock": "package",
    "out_of_stock": "no_entry",
    "error": "warning",
    "needs_check": "mag",
}


def _console(alerts: list[Alert]) -> None:
    tty = sys.stdout.isatty()
    for a in alerts:
        icon = ICONS.get(a.kind, "•")
        link = f" — {a.url}" if a.url else ""
        if tty:
            print(f"{COLORS.get(a.kind, '')}{icon} {a.message}{RESET}{link}")
        else:
            print(f"{icon} {a.message}{link}")


# Windows 10/11 toast. The text is passed through environment variables rather
# than interpolated into the script, so a product title containing a quote
# cannot break - or inject into - the command.
_WIN_TOAST_PS = """
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType=WindowsRuntime] > $null
$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
    [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$nodes = $template.GetElementsByTagName('text')
$nodes.Item(0).AppendChild($template.CreateTextNode($env:PRICEMON_TOAST_TITLE)) > $null
$nodes.Item(1).AppendChild($template.CreateTextNode($env:PRICEMON_TOAST_BODY)) > $null
$toast = [Windows.UI.Notifications.ToastNotification]::new($template)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Price Monitor').Show($toast)
"""


def _desktop_windows(title: str, body: str) -> None:
    """A toast on Windows 10/11, via whichever PowerShell is installed.

    Windows PowerShell 5.1 is tried first: it reaches the WinRT notification
    types directly, which PowerShell 7 does not do without extra assemblies.
    """
    env = {**os.environ, "PRICEMON_TOAST_TITLE": title, "PRICEMON_TOAST_BODY": body}
    for shell in ("powershell.exe", "powershell", "pwsh"):
        if not shutil.which(shell):
            continue
        proc = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-Command", _WIN_TOAST_PS],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        if proc.returncode == 0:
            return
    # No usable PowerShell: the console, phone and webhook channels still ran.


def _desktop(alerts: list[Alert]) -> None:
    """notify-send on Linux, osascript on macOS, a toast on Windows."""
    if not alerts:
        return
    title = "Price Monitor"
    body = "\n".join(f"{ICONS.get(a.kind, '•')} {a.message}" for a in alerts[:5])

    if sys.platform == "win32":
        _desktop_windows(title, body.replace("\n", "  ·  "))
        return

    if shutil.which("notify-send"):  # Linux
        urgency = (
            "critical" if any(a.kind == "target_hit" for a in alerts) else "normal"
        )
        subprocess.run(
            ["notify-send", "-u", urgency, "-a", "pricemon", title, body],
            capture_output=True,
            check=False,
        )
        return
    if sys.platform == "darwin" and shutil.which("osascript"):
        safe = body.replace("\\", "").replace('"', "'").replace("\n", " · ")
        subprocess.run(
            [
                "osascript",
                "-e",
                f'display notification "{safe}" with title "{title}" sound name "Glass"',
            ],
            capture_output=True,
            check=False,
        )


def _header_safe(text: str) -> str:
    """HTTP headers are latin-1 only.

    A product title with an em dash, an accent or an emoji in it will otherwise
    raise UnicodeEncodeError and lose the notification, so header values get
    folded down to plain ASCII. The real message goes in the body, which is
    UTF-8 and keeps every character intact.
    """
    swaps = {
        "\u2014": "-",
        "\u2013": "-",
        "\u2192": "->",
        "\u2026": "...",
        "\u00a0": " ",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
    }
    for bad, good in swaps.items():
        text = text.replace(bad, good)
    text = unicodedata.normalize("NFKD", text)
    return text.encode("ascii", "ignore").decode("ascii").strip()


def _ntfy(cfg: dict, alerts: list[Alert]) -> None:
    """Push to a phone through ntfy.sh (or your own ntfy server).

    Free and no account needed: pick a topic name, subscribe to it in the ntfy
    app, and put the same name in the config. Treat the topic as a secret -
    anyone who knows it can publish to it.
    """
    server = str(cfg.get("server") or "https://ntfy.sh").rstrip("/")
    topic = cfg.get("topic")
    if not topic:
        return
    auth = {}
    if cfg.get("token"):
        auth["Authorization"] = f"Bearer {cfg['token']}"

    def post(body: str, headers: dict[str, str]) -> bool:
        try:
            requests.post(
                f"{server}/{topic}",
                data=body.encode("utf-8"),
                headers={**headers, **auth},
                timeout=15,
            )
        except requests.RequestException as exc:
            print(f"  ntfy failed: {exc}", file=sys.stderr)
            return False
        return True

    for alert in alerts[:6]:
        headers = {
            "Title": _header_safe(alert.message)[:110] or "Price Monitor",
            "Priority": str(NTFY_PRIORITY.get(alert.kind, 3)),
            "Tags": NTFY_TAGS.get(alert.kind, "moneybag"),
            "Markdown": "no",
        }
        if alert.url:
            headers["Click"] = _header_safe(alert.url)
            headers["Actions"] = f"view, Open shop, {_header_safe(alert.url)}"
        body = alert.message + (f"\n{alert.url}" if alert.url else "")
        if not post(body, headers):
            return

    if len(alerts) > 6:
        post(
            f"and {len(alerts) - 6} more price alerts",
            {"Title": "Price Monitor", "Tags": "moneybag"},
        )


def _telegram(cfg: dict, alerts: list[Alert]) -> None:
    """Push to a phone through a Telegram bot.

    Create a bot with @BotFather, message it once, then read your chat id from
    https://api.telegram.org/bot<token>/getUpdates.
    """
    token, chat_id = cfg.get("bot_token"), cfg.get("chat_id")
    if not token or not chat_id:
        return
    lines = []
    for alert in alerts[:10]:
        icon = ICONS.get(alert.kind, "•")
        text = html.escape(alert.message)
        lines.append(
            f'{icon} <a href="{html.escape(alert.url)}">{text}</a>'
            if alert.url
            else f"{icon} {text}"
        )
    if len(alerts) > 10:
        lines.append(f"…and {len(alerts) - 10} more")
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": "\n".join(lines),
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=20,
        )
    except requests.RequestException as exc:
        print(f"  telegram failed: {exc}", file=sys.stderr)


def _webhook(url: str, alerts: list[Alert]) -> None:
    text = "\n".join(
        f"{ICONS.get(a.kind, '•')} {a.message}" + (f" — {a.url}" if a.url else "")
        for a in alerts
    )
    # "text" satisfies Slack; "content" satisfies Discord; "alerts" is for you.
    payload = {
        "text": text,
        "content": text,
        "alerts": [
            {
                "kind": a.kind,
                "product": a.product,
                "message": a.message,
                "price": a.price,
                "currency": a.currency,
                "url": a.url,
            }
            for a in alerts
        ],
    }
    try:
        requests.post(url, json=payload, timeout=15)
    except requests.RequestException as exc:
        print(f"  webhook failed: {exc}", file=sys.stderr)


def _email(cfg: dict, alerts: list[Alert]) -> None:
    msg = EmailMessage()
    msg["Subject"] = f"Price alert: {alerts[0].message[:60]}"
    msg["From"] = cfg.get("from") or cfg["user"]
    msg["To"] = cfg["to"]
    msg.set_content(
        "\n\n".join(a.message + (f"\n{a.url}" if a.url else "") for a in alerts)
    )
    try:
        with smtplib.SMTP(cfg["host"], int(cfg.get("port", 587)), timeout=30) as smtp:
            smtp.starttls()
            if cfg.get("user"):
                smtp.login(cfg["user"], cfg["password"])
            smtp.send_message(msg)
    except Exception as exc:  # noqa: BLE001 - never crash a run
        print(f"  email failed: {exc}", file=sys.stderr)


def send(alerts: list[Alert], cfg: dict) -> None:
    if not alerts:
        return
    if cfg.get("console", True):
        _console(alerts)
    if cfg.get("desktop", True):
        _desktop(alerts)
    if cfg.get("ntfy", {}).get("topic"):
        _ntfy(cfg["ntfy"], alerts)
    if cfg.get("telegram", {}).get("bot_token"):
        _telegram(cfg["telegram"], alerts)
    if cfg.get("webhook_url"):
        _webhook(cfg["webhook_url"], alerts)
    if cfg.get("email"):
        _email(cfg["email"], alerts)


def test(cfg: dict) -> None:
    send(
        [
            Alert(
                kind="target_hit",
                product="test-product",
                message="Test alert — if you can read this, the channel works",
                price=19.99,
                currency="USD",
                url="https://example.com/product",
            )
        ],
        cfg,
    )
