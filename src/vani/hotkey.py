"""Watching a physical key, without the desktop's help.

A GNOME custom shortcut bound to an XF86 media keysym silently never fires
(seen on GNOME 3.36), and XGrabKey delivered exactly one event before going
quiet. Raw XI2 events from `xinput test-xi2 --root` arrive regardless of grabs
and have been reliable, so that is what this uses.

The pipe is restarted if it dies. One caveat it also handles: an orphaned
`xinput test-xi2` from a killed daemon keeps the XI2 raw-event selection on the
root window and the next one gets BadAccess — so stale ones are swept first.
"""
from __future__ import annotations

import queue
import shutil
import subprocess
import threading
import time

XINPUT_PATTERN = "xinput test-xi2 --root"


def sweep_stale_watchers() -> None:
    """Kill orphaned xinput watchers holding the raw-event selection."""
    try:
        subprocess.run(["pkill", "-f", XINPUT_PATTERN], capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return
    time.sleep(0.5)


def available() -> bool:
    return shutil.which("xinput") is not None


class HotkeyWatcher:
    """Puts the string "key" on `events` each time the watched key is pressed."""

    def __init__(self, keycode: int, events: "queue.Queue[str]",
                 debounce_sec: float = 0.5, on_error=None):
        self.keycode = keycode
        self.events = events
        self.debounce_sec = debounce_sec
        self.on_error = on_error or (lambda msg: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not available():
            self.on_error("xinput not found — hotkey disabled")
            return
        sweep_stale_watchers()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                proc = subprocess.Popen(
                    ["xinput", "test-xi2", "--root"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            except OSError as exc:
                self.on_error(f"cannot start xinput: {exc}")
                return
            try:
                self._consume(proc)
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
            if self._stop.is_set():
                return
            self.on_error("xinput watcher exited, restarting in 2s")
            time.sleep(2)

    def _consume(self, proc: subprocess.Popen) -> None:
        """Parse the two-line RawKeyPress / detail: N event format."""
        last = 0.0
        pressed = False
        assert proc.stdout is not None
        for line in proc.stdout:
            if self._stop.is_set():
                return
            if "RawKeyPress" in line:
                pressed = True
            elif pressed and "detail:" in line:
                pressed = False
                try:
                    detail = int(line.split()[1])
                except (IndexError, ValueError):
                    continue
                now = time.time()
                if detail == self.keycode and now - last > self.debounce_sec:
                    last = now
                    self.events.put("key")
