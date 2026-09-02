"""Opening the tracker as a desktop window rather than a browser tab.

Order of preference:
  1. pywebview - a real native window, if the user installed it.
  2. A Chromium-family browser in --app mode: no tabs, no address bar, its own
     window and taskbar entry. Indistinguishable from a small desktop app.
  3. The default browser, as a last resort.
"""

from __future__ import annotations

import os
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
    # Windows names; these are not on PATH by default, see _windows_browsers()
    "chrome.exe",
    "msedge.exe",
)

MACOS_BROWSER_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Vivaldi.app/Contents/MacOS/Vivaldi",
)

WINDOWS_BROWSER_PATHS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
)


def _app_browsers() -> list[str]:
    """Browsers that can open a chromeless window, in preference order.

    On Windows and macOS these are rarely on PATH - macOS keeps them inside
    .app bundles - so their usual install locations are checked too.
    """
    found = [path for name in APP_BROWSERS if (path := shutil.which(name))]
    if sys.platform == "win32":
        found += [p for p in WINDOWS_BROWSER_PATHS if Path(p).exists()]
    elif sys.platform == "darwin":
        found += [p for p in MACOS_BROWSER_PATHS if Path(p).exists()]
    return found


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

        _name_the_app()
        webview.create_window(
            "Price Monitor", url, width=1240, height=840, min_size=(760, 560)
        )
        webview.start()
        return 0
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001 - fall back to a browser window
        print(f"native window unavailable ({exc}); using a browser window")

    for binary in _app_browsers():  # 2. app-mode browser window
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
        print(f"Price Monitor running in a {Path(binary).stem} window ({url})")
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


def _name_the_app() -> None:
    """Best effort at showing "Price Monitor" rather than "Python" in the
    macOS menu bar.

    A window opened from a Python process inherits the interpreter's bundle
    identity. Rewriting the in-memory bundle dictionary before the window is
    created is the usual remedy, but python.org framework builds run inside
    their own Python.app and may keep reporting "Python" regardless. Purely
    cosmetic either way - the window itself is titled correctly.
    """
    if sys.platform != "darwin":
        return
    try:
        from Foundation import NSBundle  # type: ignore[import-untyped]

        bundle = NSBundle.mainBundle()
        info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
        if info is not None:
            info["CFBundleName"] = "Price Monitor"
            info["CFBundleDisplayName"] = "Price Monitor"
    except Exception:  # noqa: BLE001 - a wrong name is not worth failing over
        return


def install_launcher() -> Path:
    """Put Price Monitor wherever this OS expects to find applications."""
    if sys.platform == "win32":
        return _install_windows_shortcut()
    if sys.platform == "darwin":
        return _install_macos_app()
    return _install_desktop_entry()


MACOS_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Price Monitor</string>
  <key>CFBundleDisplayName</key><string>Price Monitor</string>
  <key>CFBundleIdentifier</key><string>net.pricemon.app</string>
  <key>CFBundleVersion</key><string>{version}</string>
  <key>CFBundleShortVersionString</key><string>{version}</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>PriceMonitor</string>
  <key>CFBundleIconFile</key><string>pricemon.icns</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>LSArchitecturePriority</key>
  <array><string>arm64</string><string>x86_64</string></array>
  <key>LSMinimumSystemVersion</key><string>10.13</string>
</dict>
</plist>
"""


def _install_macos_app() -> Path:
    """Build a real .app bundle in ~/Applications.

    macOS ignores .desktop files entirely, so the Linux launcher does nothing
    here. A bundle is just a directory with the right shape: an Info.plist, an
    executable, and an icon. Written to ~/Applications rather than
    /Applications so no admin password is needed; Spotlight and Launchpad index
    both.
    """
    from . import __version__

    apps = Path.home() / "Applications"
    bundle = apps / "Price Monitor.app"
    macos_dir = bundle / "Contents" / "MacOS"
    resources = bundle / "Contents" / "Resources"
    macos_dir.mkdir(parents=True, exist_ok=True)
    resources.mkdir(parents=True, exist_ok=True)

    (bundle / "Contents" / "Info.plist").write_text(
        MACOS_PLIST.format(version=__version__)
    )

    icon = Path(__file__).parent / "assets" / "pricemon.icns"
    if icon.exists():
        shutil.copyfile(icon, resources / "pricemon.icns")

    launcher = macos_dir / "PriceMonitor"
    launcher.write_text(
        "#!/bin/bash\n"
        "# Written by `pricemon install-desktop`. Runs the tracker and opens\n"
        "# its window; quitting the window leaves the server running until you\n"
        "# quit the app.\n"
        f'cd "{Path(__file__).resolve().parent.parent}" || exit 1\n'
        "\n"
        "# A universal python launched from a bundle can start under Rosetta,\n"
        "# and then every arm64 wheel in site-packages fails to load with\n"
        '# "incompatible architecture". Pin the interpreter to the hardware\n'
        "# so the app and the terminal use the same one.\n"
        'if [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = "1" ] \\\n'
        "   && /usr/bin/arch -arm64 /usr/bin/true 2>/dev/null; then\n"
        f'  exec /usr/bin/arch -arm64 "{sys.executable}" -m pricemon app\n'
        "fi\n"
        f'exec "{sys.executable}" -m pricemon app\n'
    )
    launcher.chmod(0o755)

    # Nudge Finder, and register with Launch Services so Spotlight and
    # Launchpad index the bundle straight away rather than whenever they next
    # happen to rescan ~/Applications.
    subprocess.run(["touch", str(bundle)], capture_output=True, check=False)
    lsregister = Path(
        "/System/Library/Frameworks/CoreServices.framework/Frameworks"
        "/LaunchServices.framework/Support/lsregister"
    )
    if lsregister.exists():
        subprocess.run(
            [str(lsregister), "-f", str(bundle)], capture_output=True, check=False
        )
    return bundle


def _install_windows_shortcut() -> Path:
    """A Start Menu shortcut pointing at a .cmd that launches the app."""
    launcher = config_mod.home() / "pricemon-app.cmd"
    python = Path(sys.executable)
    quiet = python.with_name("pythonw.exe")
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text(
        "\r\n".join(
            [
                "@echo off",
                f'cd /d "{Path(__file__).resolve().parent.parent}"',
                f'"{quiet if quiet.exists() else python}" -m pricemon app',
            ]
        )
        + "\r\n",
        encoding="utf-8",
    )

    start_menu = (
        Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
        / "Microsoft/Windows/Start Menu/Programs"
    )
    start_menu.mkdir(parents=True, exist_ok=True)
    shortcut = start_menu / "Price Monitor.lnk"
    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut("
        f"'{shortcut}'); $s.TargetPath = '{launcher}'; "
        f"$s.WorkingDirectory = '{launcher.parent}'; "
        "$s.Description = 'Track product prices'; $s.Save()"
    )
    for shell in ("powershell.exe", "powershell", "pwsh"):
        if shutil.which(shell):
            subprocess.run(
                [shell, "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                check=False,
            )
            break
    return shortcut if shortcut.exists() else launcher


def _install_desktop_entry() -> Path:
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
