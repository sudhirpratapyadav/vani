"""Service lifecycle: the systemd user units behind the app.

vani runs as two units — `vani-daemon.service` (the microphone loop) and
`vani-tray.service` (the indicator) — plus `ydotoold.service` on Wayland.
This module is the one place that knows their names and drives systemctl, so
the CLI (`vani service`, `vani quit`), the tray's Settings menu, and doctor
all agree on what "running", "start on login", and "quit" mean.

Everything degrades: without systemd (or with the units not installed) the
daemon is found via its pidfile and stopped with SIGTERM, so `vani quit`
works in a plain `vani start` terminal session too.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess

from . import paths, state

DAEMON_UNIT = "vani-daemon.service"
TRAY_UNIT = "vani-tray.service"
UNITS = (DAEMON_UNIT, TRAY_UNIT)


def _systemctl(*args: str) -> "tuple[int, str]":
    if not shutil.which("systemctl"):
        return 127, "systemctl not found"
    try:
        proc = subprocess.run(["systemctl", "--user", *args],
                              capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def unit_state(unit: str) -> "tuple[str, str]":
    """(active-state, enabled-state) — e.g. ("active", "enabled")."""
    _, active = _systemctl("is-active", unit)
    _, enabled = _systemctl("is-enabled", unit)
    return active or "unknown", enabled or "unknown"


def units_installed() -> bool:
    code, out = _systemctl("cat", DAEMON_UNIT)
    return code == 0 and bool(out)


def starts_on_login() -> bool:
    return unit_state(DAEMON_UNIT)[1] == "enabled"


def set_start_on_login(enabled: bool) -> "tuple[bool, str]":
    """Enable or disable starting at login. Does not start or stop anything now."""
    code, out = _systemctl("enable" if enabled else "disable", *UNITS)
    return code == 0, out


def control(action: str) -> "tuple[bool, str]":
    """start / stop / restart the vani units together."""
    assert action in ("start", "stop", "restart")
    code, out = _systemctl(action, *UNITS)
    return code == 0, out


def quit_all() -> "list[str]":
    """Stop vani completely — daemon and tray, however they were started.

    The tray unit is stopped last: when the tray itself triggers this, the
    daemon must already be down by the time the tray process disappears.
    """
    done: "list[str]" = []
    if units_installed():
        for unit in (DAEMON_UNIT, TRAY_UNIT):
            if unit_state(unit)[0] == "active":
                ok, out = _systemctl("stop", unit)
                done.append(f"stopped {unit}" if ok else f"could not stop {unit}: {out}")
    # A daemon started by hand in a terminal has no unit; find it by pidfile.
    pid = state.read_pidfile(paths.daemon_pidfile())
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
            done.append(f"stopped daemon (pid {pid})")
        except OSError as exc:
            done.append(f"could not signal pid {pid}: {exc}")
    if not done:
        done.append("nothing was running")
    return done
