"""Shared state: the status file and the transcript history.

The daemon, the toggle command, and the tray are separate processes; a tiny
file in $XDG_RUNTIME_DIR is how they agree on what is happening. Reads never
raise — a missing or garbled file simply means "idle".
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from . import paths

IDLE = "idle"
RECORDING = "recording"
TRANSCRIBING = "transcribing"
SILENCE = "silence"  # written as "silence:<seconds remaining>"


def set_status(state: str) -> None:
    path = paths.status_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(state)
        tmp.replace(path)  # atomic: the tray never reads a half-written file
    except OSError:
        pass


def set_countdown(seconds: float) -> None:
    set_status(f"{SILENCE}:{seconds:.1f}")


def read_status() -> tuple[str, float]:
    """Return (state, countdown_seconds). Countdown is 0 unless state is 'silence'."""
    try:
        raw = paths.status_file().read_text().strip()
    except OSError:
        return IDLE, 0.0
    if raw.startswith(SILENCE + ":"):
        try:
            return SILENCE, float(raw.split(":", 1)[1])
        except ValueError:
            return SILENCE, 0.0
    return raw if raw in (IDLE, RECORDING, TRANSCRIBING) else IDLE, 0.0


def append_history(text: str) -> None:
    path = paths.history_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write("%s\t%s\n" % (time.strftime("%F %T"), text.replace("\t", " ")))
    except OSError:
        pass


def read_history(limit: int | None = None) -> list[tuple[str, str]]:
    """Most recent first, as (timestamp, text) pairs."""
    try:
        lines = paths.history_file().read_text().splitlines()
    except OSError:
        return []
    entries = []
    for line in lines:
        if not line.strip():
            continue
        stamp, _, text = line.partition("\t")
        entries.append((stamp, text) if text else ("", stamp))
    entries.reverse()
    return entries[:limit] if limit else entries


# --------------------------------------------------------------------------
# Pidfiles


def write_pidfile(path: Path, pid: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(pid if pid is not None else os.getpid()))


def read_pidfile(path: Path) -> int | None:
    """The pid in the file, but only if that process is still alive."""
    try:
        pid = int(path.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:  # exists, owned by someone else
        return pid
    return pid


def clear_pidfile(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
