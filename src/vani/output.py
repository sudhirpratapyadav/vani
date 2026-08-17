"""Delivering the transcript to the focused application.

X11 gets `xdotool`, Wayland gets `ydotool` (which needs its daemon and access
to /dev/uinput). When neither is usable the text goes to the clipboard, which
is a poor substitute for typing but much better than losing the transcript.
"""
from __future__ import annotations

import os
import shutil
import subprocess


class OutputError(Exception):
    """The transcript could not be delivered."""


def session_type() -> str:
    """'wayland', 'x11', or 'unknown'."""
    kind = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if kind in ("wayland", "x11"):
        return kind
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return "unknown"


def detect_typer() -> str:
    """Pick the best available backend for this session."""
    if session_type() == "wayland" and shutil.which("ydotool"):
        return "ydotool"
    if shutil.which("xdotool"):
        return "xdotool"
    if shutil.which("ydotool"):
        return "ydotool"
    if clipboard_command():
        return "clipboard"
    return "stdout"


def clipboard_command() -> list[str] | None:
    if session_type() == "wayland" and shutil.which("wl-copy"):
        return ["wl-copy"]
    if shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard"]
    if shutil.which("xsel"):
        return ["xsel", "--clipboard", "--input"]
    if shutil.which("wl-copy"):
        return ["wl-copy"]
    return None


class Typist:
    """Types text into the focused field using the configured backend."""

    def __init__(self, backend: str = "auto", delay_ms: int = 2):
        self.backend = detect_typer() if backend == "auto" else backend
        self.delay_ms = delay_ms

    def deliver(self, text: str) -> str:
        """Send `text` to the desktop; returns the backend that handled it."""
        if not text:
            return self.backend
        handler = {
            "xdotool": self._xdotool,
            "ydotool": self._ydotool,
            "clipboard": self._clipboard,
            "stdout": self._stdout,
        }.get(self.backend)
        if handler is None:
            raise OutputError(f"unknown output backend {self.backend!r}")
        handler(text)
        return self.backend

    def _run(self, cmd: list[str]) -> None:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except FileNotFoundError:
            raise OutputError(f"{cmd[0]} is not installed") from None
        except subprocess.SubprocessError as exc:
            raise OutputError(f"{cmd[0]}: {exc}") from None
        if proc.returncode != 0:
            raise OutputError(f"{cmd[0]}: {proc.stderr.strip()[:120] or 'failed'}")

    def _xdotool(self, text: str) -> None:
        self._run(["xdotool", "type", "--clearmodifiers",
                   "--delay", str(self.delay_ms), "--", text])

    def _ydotool(self, text: str) -> None:
        self._run(["ydotool", "type", "--key-delay", str(self.delay_ms), "--", text])

    def _clipboard(self, text: str) -> None:
        cmd = clipboard_command()
        if cmd is None:
            raise OutputError("no clipboard tool (install xclip or wl-clipboard)")
        try:
            subprocess.run(cmd, input=text, text=True, timeout=10, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            raise OutputError(f"{cmd[0]}: {exc}") from None

    def _stdout(self, text: str) -> None:
        print(text, flush=True)
