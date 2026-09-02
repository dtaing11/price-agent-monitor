"""Opening the tracker as a desktop window rather than a browser tab.

Order of preference:
  1. pywebview - a real native window, if the user installed it.
  2. A Chromium-family browser in --app mode: no tabs, no address bar, its own
     window and taskbar entry. Indistinguishable from a small desktop app.
  3. The default browser, as a last resort.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from . import config as config_mod
from .webapp import free_port, serve

APP_BROWSERS = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "brave-browser",
    "microsoft-edge",
    "microsoft-edge-stable",
    "vivaldi",
)

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64">
  <rect width="64" height="64" rx="14" fill="#1f2a44"/>
  <polyline points="12,44 24,32 34,38 52,18" fill="none" stroke="#5fbd8a"
            stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="52" cy="18" r="4.5" fill="#5fbd8a"/>
</svg>
"""


def _serve_background(port: int) -> threading.Thread:
    thread = threading.Thread(
        target=serve, kwargs={"port": port, "open_browser": False}, daemon=True
    )
    thread.start()
    time.sleep(0.8)  # let the socket come up before pointing a window at it
    return thread


def launch(port: int = 8787) -> int:
    port = free_port(port)
    url = f"http://127.0.0.1:{port}/"
    _serve_background(port)

    try:  # 1. native window
        import webview  # type: ignore[import-not-found]

        webview.create_window(
            "Price Monitor", url, width=1240, height=840, min_size=(760, 560)
        )
        webview.start()
        return 0
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001 - fall back to a browser window
        print(f"native window unavailable ({exc}); using a browser window")

    for browser in APP_BROWSERS:  # 2. app-mode browser window
        binary = shutil.which(browser)
        if not binary:
            continue
        profile = config_mod.home() / "browser-profile"
        profile.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(
            [
                binary,
                f"--app={url}",
                f"--user-data-dir={profile}",
                "--window-size=1240,840",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"Price Monitor running in a {browser} window ({url})")
        print("close the window, or press ctrl-c here, to quit")
        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
        return 0

    print(f"opening {url} in your default browser (ctrl-c here to quit)")
    webbrowser.open(url)  # 3. plain browser
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


def install_launcher() -> Path:
    """Write a .desktop entry so the app shows up in the applications menu."""
    apps = Path.home() / ".local/share/applications"
    icons = Path.home() / ".local/share/icons/hicolor/scalable/apps"
    apps.mkdir(parents=True, exist_ok=True)
    icons.mkdir(parents=True, exist_ok=True)

    icon_path = icons / "pricemon.svg"
    icon_path.write_text(ICON_SVG)

    launcher = apps / "pricemon.desktop"
    project = Path(__file__).resolve().parent.parent
    runner = project / "bin" / "pricemon"
    exec_cmd = str(runner) if runner.exists() else f"{sys.executable} -m pricemon"
    launcher.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Price Monitor\n"
        "Comment=Track product prices and get alerted when they drop\n"
        f"Exec={exec_cmd} app\n"
        f"Path={project}\n"
        "Icon=pricemon\n"
        "Terminal=false\n"
        "Categories=Utility;Network;\n"
        "Keywords=price;deal;shopping;tracker;\n"
        "StartupNotify=true\n"
    )
    launcher.chmod(0o755)
    if shutil.which("update-desktop-database"):
        subprocess.run(
            ["update-desktop-database", str(apps)], capture_output=True, check=False
        )
    return launcher
