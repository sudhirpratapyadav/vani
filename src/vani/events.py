"""The daemon → UI event channel.

The daemon publishes everything the interface needs — state changes, the mic
level, live text, countdowns, results, errors — as newline-JSON over a Unix
socket in $XDG_RUNTIME_DIR/vani/. The UI process subscribes and owns all
presentation; whether anyone is subscribed is also how the daemon decides
between staying quiet and falling back to desktop notifications.

Files (state.py) remain the coarse fallback — `vani status` and a UI that
outlives a daemon restart still read them — but anything fast (the 8 Hz level
feed for the waveform) or transient (an error with its message) exists only
on this channel. Push, not polling, is what makes the pill possible.

Every message is one JSON object per line with at least {"ev": <name>}.
A new subscriber first receives a snapshot of the current state, so the UI
never starts blind.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from typing import Callable

from . import paths

#: A subscriber that cannot take a message within this long is dropped —
#: publishing must never stall the microphone loop.
SEND_TIMEOUT = 0.5


class EventBus:
    """Server side: accepts subscribers, fans events out to them."""

    def __init__(self, snapshot: "Callable[[], list[dict]] | None" = None):
        self._snapshot = snapshot or (lambda: [])
        self._subs: "list[socket.socket]" = []
        self._lock = threading.Lock()
        self._server: "socket.socket | None" = None

    def start(self) -> None:
        path = paths.events_socket()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.unlink()
        except OSError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        os.chmod(path, 0o600)
        server.listen(4)
        self._server = server
        threading.Thread(target=self._accept_loop, daemon=True).start()

    @property
    def has_subscribers(self) -> bool:
        with self._lock:
            return bool(self._subs)

    def publish(self, ev: str, **fields) -> None:
        self._send_all(_encode(ev, fields))

    def close(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        with self._lock:
            subs, self._subs = self._subs, []
        for sub in subs:
            try:
                sub.close()
            except OSError:
                pass
        try:
            paths.events_socket().unlink()
        except OSError:
            pass

    # -- internals ---------------------------------------------------------

    def _accept_loop(self) -> None:
        while self._server is not None:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return  # closed
            conn.settimeout(SEND_TIMEOUT)
            try:
                for event in self._snapshot():
                    conn.sendall(_encode(event.pop("ev"), event))
            except (OSError, KeyError):
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            with self._lock:
                self._subs.append(conn)

    def _send_all(self, payload: bytes) -> None:
        with self._lock:
            subs = list(self._subs)
        dead = []
        for sub in subs:
            try:
                sub.sendall(payload)
            except OSError:
                dead.append(sub)
        if dead:
            with self._lock:
                for sub in dead:
                    if sub in self._subs:
                        self._subs.remove(sub)
            for sub in dead:
                try:
                    sub.close()
                except OSError:
                    pass


def _encode(ev: str, fields: dict) -> bytes:
    return (json.dumps({"ev": ev, **fields}) + "\n").encode()


class EventClient:
    """UI side: connect, deliver events to a callback, reconnect forever.

    `on_event` is called from the reader thread with each decoded dict;
    `on_connect` / `on_disconnect` bracket each connection so the caller can
    switch between event-driven and file-polling modes.
    """

    def __init__(self, on_event: Callable[[dict], None],
                 on_connect: Callable[[], None] = lambda: None,
                 on_disconnect: Callable[[], None] = lambda: None):
        self.on_event = on_event
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self.connected = False
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(str(paths.events_socket()))
            except OSError:
                sock.close()
                if self._stop.wait(1.0):
                    return
                continue
            self.connected = True
            self.on_connect()
            try:
                self._read(sock)
            finally:
                self.connected = False
                self.on_disconnect()
                try:
                    sock.close()
                except OSError:
                    pass
            self._stop.wait(0.5)  # the daemon restarting; don't spin

    def _read(self, sock: socket.socket) -> None:
        buf = b""
        sock.settimeout(1.0)
        while not self._stop.is_set():
            try:
                data = sock.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            if not data:
                return  # daemon gone
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                try:
                    self.on_event(event)
                except Exception:
                    pass  # a UI bug must not kill the reader


def now_ms() -> int:
    return int(time.time() * 1000)
