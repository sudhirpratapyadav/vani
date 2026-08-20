"""Watching a physical key, without the desktop's help.

Two backends, one protocol: the watcher puts ("down", timestamp) and
("up", timestamp) tuples on the queue, and the daemon turns those into
gestures — tap, hold-to-talk, hold-to-cancel. Press *and* release matter now:
push-to-talk is a quasimode, maintained by the finger, and the release is the
send.

- **xinput** (X11): raw XI2 events from `xinput test-xi2 --root`. A GNOME
  custom shortcut bound to an XF86 media keysym silently never fires (seen on
  GNOME 3.36), and XGrabKey delivered exactly one event before going quiet;
  the raw event stream has been reliable. X autorepeat arrives as synthetic
  release+press pairs, so a release is held briefly and cancelled if a press
  follows immediately — otherwise a held key would look like a drumroll.

- **evdev** (Wayland, or anywhere X can't see the key): /dev/input/event*
  read directly, which reports clean down/up/repeat values. Needs read access
  to the devices — membership in the `input` group, which the Wayland typing
  setup (ydotool) already requires. Config keycodes are X keycodes for
  backward compatibility; evdev keycodes are X minus 8.

The pipe/devices are restarted if they die. One caveat also handled: an
orphaned `xinput test-xi2` from a killed daemon keeps the XI2 raw-event
selection on the root window and the next one gets BadAccess — so stale ones
are swept first.
"""
from __future__ import annotations

import glob
import os
import queue
import select
import shutil
import struct
import subprocess
import threading
import time

XINPUT_PATTERN = "xinput test-xi2 --root"

#: X keycodes are evdev keycodes shifted by 8 (the historical X offset).
X_TO_EVDEV_OFFSET = 8

#: An X autorepeat release is followed by its press within a few ms; anything
#: this close is treated as one continuous hold.
AUTOREPEAT_WINDOW = 0.08

_EV_KEY = 0x01
_EVENT_FORMAT = "llHHi"  # struct input_event: timeval, type, code, value
_EVENT_SIZE = struct.calcsize(_EVENT_FORMAT)


def sweep_stale_watchers() -> None:
    """Kill orphaned xinput watchers holding the raw-event selection."""
    try:
        subprocess.run(["pkill", "-f", XINPUT_PATTERN], capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return
    time.sleep(0.5)


def available() -> bool:
    return shutil.which("xinput") is not None


def evdev_readable() -> bool:
    """Can we read raw input devices at all?"""
    return any(os.access(path, os.R_OK)
               for path in glob.glob("/dev/input/event*"))


def build(cfg_hotkey, events: "queue.Queue", on_error=None):
    """Pick a watcher for this session; returns it unstarted, or None.

    X11 keeps the proven xinput pipe; everywhere else (Wayland has no global
    X key events) evdev is the only thing that can see the key.
    """
    from .output import session_type

    backend = cfg_hotkey.backend
    if backend == "auto":
        if session_type() == "x11" and available():
            backend = "xinput"
        elif evdev_readable():
            backend = "evdev"
        elif available():
            backend = "xinput"
        else:
            backend = "evdev"  # start() will explain what's missing
    if backend == "evdev":
        return EvdevWatcher(cfg_hotkey.keycode, events,
                            cfg_hotkey.debounce_sec, on_error=on_error)
    return HotkeyWatcher(cfg_hotkey.keycode, events,
                         cfg_hotkey.debounce_sec, on_error=on_error)


class _BaseWatcher:
    def __init__(self, keycode: int, events: "queue.Queue",
                 debounce_sec: float = 0.5, on_error=None):
        self.keycode = keycode
        self.events = events
        self.debounce_sec = debounce_sec
        self.on_error = on_error or (lambda msg: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._down = False
        self._last_down = 0.0

    def stop(self) -> None:
        self._stop.set()

    def _emit_down(self) -> None:
        now = time.time()
        if self._down or now - self._last_down < self.debounce_sec:
            return
        self._down = True
        self._last_down = now
        self.events.put(("down", now))

    def _emit_up(self) -> None:
        if not self._down:
            return
        self._down = False
        self.events.put(("up", time.time()))


class HotkeyWatcher(_BaseWatcher):
    """The xinput backend. Name kept from the press-only era."""

    backend = "xinput"

    def start(self) -> None:
        if not available():
            self.on_error("xinput not found — hotkey disabled")
            return
        sweep_stale_watchers()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

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
        """Parse the two-line RawKeyPress/Release + detail: N event format."""
        pending = ""  # which event type the next detail: line belongs to
        release_timer: "threading.Timer | None" = None
        lock = threading.Lock()
        assert proc.stdout is not None

        def flush_release() -> None:
            with lock:
                self._emit_up()

        for line in proc.stdout:
            if self._stop.is_set():
                return
            if "RawKeyPress" in line:
                pending = "press"
            elif "RawKeyRelease" in line:
                pending = "release"
            elif pending and "detail:" in line:
                kind, pending = pending, ""
                try:
                    detail = int(line.split()[1])
                except (IndexError, ValueError):
                    continue
                if detail != self.keycode:
                    continue
                with lock:
                    if kind == "press":
                        if release_timer is not None and release_timer.is_alive():
                            # Release+press within the window: X autorepeat.
                            # The key never left the user's finger.
                            release_timer.cancel()
                            release_timer = None
                            continue
                        self._emit_down()
                    else:
                        release_timer = threading.Timer(
                            AUTOREPEAT_WINDOW, flush_release)
                        release_timer.daemon = True
                        release_timer.start()
        if release_timer is not None:
            release_timer.cancel()


class EvdevWatcher(_BaseWatcher):
    """Raw /dev/input reader: works on Wayland, reports clean down/up."""

    backend = "evdev"
    RESCAN_SEC = 10.0  # picks up hot-plugged keyboards (Bluetooth, docks)

    def __init__(self, keycode: int, events: "queue.Queue",
                 debounce_sec: float = 0.5, on_error=None):
        super().__init__(keycode, events, debounce_sec, on_error)
        self.evdev_code = keycode - X_TO_EVDEV_OFFSET

    def start(self) -> None:
        if not evdev_readable():
            self.on_error(
                "no readable /dev/input devices — the hotkey needs membership "
                "in the 'input' group (sudo usermod -aG input $USER, re-login)")
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _open_all(self) -> "dict[int, str]":
        fds: "dict[int, str]" = {}
        for path in sorted(glob.glob("/dev/input/event*")):
            try:
                fds[os.open(path, os.O_RDONLY | os.O_NONBLOCK)] = path
            except OSError:
                continue
        return fds

    def _run(self) -> None:
        warned = False
        while not self._stop.is_set():
            fds = self._open_all()
            if not fds:
                if not warned:
                    warned = True
                    self.on_error("no readable input devices; retrying")
                if self._stop.wait(5):
                    return
                continue
            warned = False
            opened_at = time.time()
            try:
                while not self._stop.is_set():
                    if time.time() - opened_at > self.RESCAN_SEC:
                        break  # reopen to notice appeared/vanished devices
                    try:
                        ready, _, _ = select.select(list(fds), [], [], 1.0)
                    except OSError:
                        break
                    for fd in ready:
                        if not self._drain(fd):
                            fds.pop(fd, None)
                            try:
                                os.close(fd)
                            except OSError:
                                pass
                    if not fds:
                        break
            finally:
                for fd in fds:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def _drain(self, fd: int) -> bool:
        """Read pending events from one device; False when it died."""
        try:
            data = os.read(fd, _EVENT_SIZE * 64)
        except BlockingIOError:
            return True
        except OSError:
            return False
        for off in range(0, len(data) - len(data) % _EVENT_SIZE, _EVENT_SIZE):
            _s, _u, etype, code, value = struct.unpack_from(
                _EVENT_FORMAT, data, off)
            if etype != _EV_KEY or code != self.evdev_code:
                continue
            if value == 1:
                self._emit_down()
            elif value == 0:
                self._emit_up()
            # value == 2 is autorepeat: the key is simply still held.
        return True
