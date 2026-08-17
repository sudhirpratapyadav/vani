"""Desktop notifications.

The countdown replaces its own notification in place instead of stacking a new
banner every 0.3 s, which needs the notification id back from the daemon —
`notify-send` doesn't return it, so this goes over the D-Bus interface with
`gdbus`. If gdbus is missing or notifications are disabled the calls become
no-ops; nothing else in the app needs to care.
"""
from __future__ import annotations

import re
import shutil
import subprocess

APP_NAME = "vani"
TITLE = "vani"


class Notifier:
    """Shows a single notification slot that can be updated and closed."""

    def __init__(self, enabled: bool = True, title: str = TITLE):
        self.title = title
        self.enabled = enabled and shutil.which("gdbus") is not None
        self._id = 0

    def _call(self, *args: str) -> str:
        try:
            return subprocess.run(
                ["gdbus", "call", "--session",
                 "--dest", "org.freedesktop.Notifications",
                 "--object-path", "/org/freedesktop/Notifications", *args],
                capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.SubprocessError):
            return ""

    def show(self, body: str, ms: int = 3000, *, replace: bool = False) -> None:
        """Post a notification; `replace=True` updates the previous one in place."""
        if not self.enabled:
            return
        out = self._call(
            "--method", "org.freedesktop.Notifications.Notify",
            APP_NAME, str(self._id if replace else 0), "audio-input-microphone",
            self.title, body, "[]", "{}", str(ms))
        m = re.search(r"uint32 (\d+)", out)
        if m:
            self._id = int(m.group(1))

    def close(self) -> None:
        if self.enabled and self._id:
            self._call("--method", "org.freedesktop.Notifications.CloseNotification",
                       str(self._id))
            self._id = 0


class NullNotifier(Notifier):
    """Used in tests and --test-wav runs."""

    def __init__(self) -> None:
        super().__init__(enabled=False)
        self.messages: list[str] = []

    def show(self, body: str, ms: int = 3000, *, replace: bool = False) -> None:
        self.messages.append(body)

    def close(self) -> None:
        pass
