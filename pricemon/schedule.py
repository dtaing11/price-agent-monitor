"""Running the agent twice a day, on whichever OS you are using.

Linux and macOS get a crontab block; Windows gets Scheduled Tasks. Both are
driven from the same `pricemon install-cron` command, and both run the check
through a small generated script rather than a long inline command - schtasks
in particular mangles quoting in anything complicated, and a script is
something you can read and run by hand when a scheduled run misbehaves.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from . import config as config_mod
from . import cron

TASK_PREFIX = "PriceMonitor"


def is_windows() -> bool:
    return sys.platform == "win32"


def project_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def log_file() -> Path:
    return config_mod.home() / "cron.log"


# --------------------------------------------------------------------------
# Windows
# --------------------------------------------------------------------------
def _python_for_tasks() -> str:
    """pythonw.exe runs without opening a console window."""
    exe = Path(sys.executable)
    quiet = exe.with_name("pythonw.exe")
    return str(quiet if quiet.exists() else exe)


def write_runner_script() -> Path:
    """A .cmd the scheduled task invokes, so quoting stays sane."""
    script = config_mod.home() / "run-check.cmd"
    home = os.environ.get("PRICEMON_HOME")
    lines = [
        "@echo off",
        "rem Written by `pricemon install-cron`. Safe to run by hand to test.",
        f'cd /d "{project_dir()}"',
    ]
    if home:
        lines.append(f'set "PRICEMON_HOME={home}"')
    lines.append(
        f'"{_python_for_tasks()}" -m pricemon check --quiet >> "{log_file()}" 2>&1'
    )
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    return script


def windows_task_name(hour: int, minute: int) -> str:
    return f"{TASK_PREFIX} {hour:02d}{minute:02d}"


def windows_commands(times: list[tuple[int, int]], script: Path) -> list[list[str]]:
    """The schtasks invocations for these times - also used for --dry-run."""
    return [
        [
            "schtasks",
            "/Create",
            "/TN",
            windows_task_name(hour, minute),
            "/TR",
            f'"{script}"',
            "/SC",
            "DAILY",
            "/ST",
            f"{hour:02d}:{minute:02d}",
            "/F",  # replace an existing task of the same name
        ]
        for hour, minute in times
    ]


def _windows_existing_tasks() -> list[str]:
    proc = subprocess.run(
        ["schtasks", "/Query", "/FO", "LIST"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return []
    return [
        line.split(":", 1)[1].strip().lstrip("\\")
        for line in proc.stdout.splitlines()
        if line.lower().startswith("taskname:") and TASK_PREFIX in line
    ]


def _windows_install(times: list[tuple[int, int]]) -> str:
    script = write_runner_script()
    _windows_uninstall()  # drop tasks from a previous run, times may have changed
    for command in windows_commands(times, script):
        proc = subprocess.run(command, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(
                f"schtasks failed for {command[4]}: "
                f"{(proc.stderr or proc.stdout).strip()[:200]}"
            )
    listed = "\n".join(
        f"  {windows_task_name(h, m)}  daily at {h:02d}:{m:02d}" for h, m in times
    )
    return f"{listed}\n\n  runner script: {script}"


def _windows_uninstall() -> bool:
    removed = False
    for name in _windows_existing_tasks():
        proc = subprocess.run(
            ["schtasks", "/Delete", "/TN", name, "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        removed = removed or proc.returncode == 0
    return removed


# --------------------------------------------------------------------------
# One entry point for both worlds
# --------------------------------------------------------------------------
def preview(times: list[tuple[int, int]]) -> str:
    if is_windows():
        script = config_mod.home() / "run-check.cmd"
        return "\n".join(
            " ".join(part if " " not in part else f'"{part}"' for part in command)
            for command in windows_commands(times, script)
        )
    return cron.render_block(cron.build_lines(times, project_dir(), log_file()))


def install(times: list[tuple[int, int]]) -> str:
    if is_windows():
        return _windows_install(times)
    return cron.install(times, project_dir(), log_file())


def uninstall() -> bool:
    return _windows_uninstall() if is_windows() else cron.uninstall()


def status() -> dict:
    """What is scheduled right now, for display in the app."""
    times: list[str] = []
    if is_windows():
        for name in _windows_existing_tasks():
            digits = name.replace(TASK_PREFIX, "").strip()
            if len(digits) == 4 and digits.isdigit():
                times.append(f"{digits[:2]}:{digits[2:]}")
    else:
        for line in cron.current_crontab().splitlines():
            if "pricemon check --quiet" not in line:
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                times.append(f"{int(parts[1]):02d}:{int(parts[0]):02d}")
    times.sort()
    return {
        "installed": bool(times),
        "times": times,
        "mechanism": "Windows Task Scheduler" if is_windows() else "cron",
        "log": str(log_file()),
        "verify": verify_hint(),
    }


def verify_hint() -> str:
    return 'schtasks /Query /TN "PriceMonitor 0800"' if is_windows() else "crontab -l"
