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


def set_live(text: str) -> None:
    """Publish the current recording's transcript-so-far for the overlay."""
    path = paths.live_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text)
        tmp.replace(path)
    except OSError:
        pass


def read_live() -> str:
    try:
        return paths.live_file().read_text()
    except OSError:
        return ""


def set_server(ok: bool, detail: str = "") -> None:
    """Record the last health-check verdict for the tray and `vani status`."""
    path = paths.server_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(("ok" if ok else "down") + ("\t" + detail if detail else ""))
        tmp.replace(path)
    except OSError:
        pass


def read_server() -> "tuple[bool | None, str]":
    """(ok, detail); ok is None when no health check has run yet."""
    try:
        raw = paths.server_file().read_text().strip()
    except OSError:
        return None, ""
    verdict, _, detail = raw.partition("\t")
    if verdict not in ("ok", "down"):
        return None, ""
    return verdict == "ok", detail


def append_history(text: str) -> None:
    path = paths.history_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write("%s\t%s\n" % (time.strftime("%F %T"), text.replace("\t", " ")))
    except OSError:
        pass


def save_last_wav(wav: bytes) -> bool:
    """Keep the last clip for debugging. False if it could not be written.

    Never raises: a full or read-only cache directory must not turn a
    successful transcription into a traceback just before it is typed.
    """
    try:
        path = paths.last_wav()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(wav)
        return True
    except OSError:
        return False


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
