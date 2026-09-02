"""Managing the crontab block that runs the agent on a schedule.

Cron gives a job almost no environment: no PATH to your tools, no session bus
for desktop notifications.  Everything the run needs is therefore written
inline into the command, not as crontab-level variables (which would leak into
any entries below ours).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple

BEGIN = "# >>> pricemon >>>  (managed by `pricemon install-cron` - edit with that command)"
END = "# <<< pricemon <<<"


def parse_times(spec: str) -> List[Tuple[int, int]]:
    """'08:00,20:00' -> [(8, 0), (20, 0)]"""
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        hh, _, mm = part.partition(":")
        hour, minute = int(hh), int(mm or 0)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"invalid time {part!r}")
        out.append((hour, minute))
    if not out:
        raise ValueError("no times given")
    return out


def _runner_env() -> str:
    """Inline env so cron can find python, claude, and the desktop session."""
    paths = ["/usr/local/bin", "/usr/bin", "/bin"]
    for tool in ("claude", "python3"):
        found = shutil.which(tool)
        if found:
            d = str(Path(found).parent)
            if d not in paths:
                paths.insert(0, d)
    home = str(Path.home())
    for extra in (f"{home}/.local/bin", f"{home}/.npm-global/bin", "/opt/homebrew/bin"):
        if Path(extra).is_dir() and extra not in paths:
            paths.insert(0, extra)

    parts = [f'PATH="{":".join(paths)}"', f'HOME="{home}"']
    # Needed for notify-send under cron on Linux desktops.
    uid = os.getuid()
    if sys.platform.startswith("linux"):
        parts += ['DISPLAY="${DISPLAY:-:0}"',
                  f'DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/{uid}/bus"']
    pricemon_home = os.environ.get("PRICEMON_HOME")
    if pricemon_home:
        parts.append(f'PRICEMON_HOME="{pricemon_home}"')
    return " ".join(parts)


def build_lines(times: List[Tuple[int, int]], project_dir: Path, log_file: Path) -> List[str]:
    env = _runner_env()
    py = sys.executable or "python3"
    lines = []
    for hour, minute in times:
        lines.append(
            f'{minute} {hour} * * * cd "{project_dir}" && {env} '
            f'{py} -m pricemon check --quiet >> "{log_file}" 2>&1'
        )
    return lines


def current_crontab() -> str:
    proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if proc.returncode != 0:
        return ""            # "no crontab for user" is not an error for us
    return proc.stdout


def strip_block(crontab: str) -> str:
    out, skipping = [], False
    for line in crontab.splitlines():
        if line.strip() == BEGIN.strip() or line.startswith("# >>> pricemon >>>"):
            skipping = True
            continue
        if line.strip() == END:
            skipping = False
            continue
        if not skipping:
            out.append(line)
    return "\n".join(out).rstrip("\n")


def render_block(lines: List[str]) -> str:
    return "\n".join([BEGIN, *lines, END])


def write_crontab(text: str) -> None:
    if not shutil.which("crontab"):
        raise RuntimeError("`crontab` not found on this system")
    proc = subprocess.run(["crontab", "-"], input=text.rstrip("\n") + "\n",
                          text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"crontab install failed: {proc.stderr.strip()}")


def install(times: List[Tuple[int, int]], project_dir: Path, log_file: Path) -> str:
    block = render_block(build_lines(times, project_dir, log_file))
    existing = strip_block(current_crontab())
    combined = (existing + "\n\n" if existing else "") + block
    write_crontab(combined)
    return block


def uninstall() -> bool:
    existing = current_crontab()
    stripped = strip_block(existing)
    if stripped == existing.rstrip("\n"):
        return False
    write_crontab(stripped)
    return True
