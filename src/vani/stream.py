"""Live transcription over the realtime ASR WebSocket.

Batch dictation sends one clip up and gets one transcript back; this is the
same Voxtral server's streaming face. Chunks go up while the user is still
talking and words come back 0.3-0.5 s behind the speech, so the desktop can
show the text forming before the recording has even stopped. The exchange:

    -> session.update            {"model": ...}
    -> input_audio_buffer.commit                 starts the generation task
    -> input_audio_buffer.append {"audio": <base64 PCM16>}   ... repeatedly
    -> input_audio_buffer.commit {"final": true}             ends the utterance
    <- transcription.delta       {"delta": " word"}
    <- transcription.done        {"text": "the whole thing"}

The daemon is deliberately synchronous, so this is built on the sync
`websockets` client with two small threads per recording — a pump that drains
a queue into the socket, and a reader that collects deltas — rather than
dragging asyncio into the main loop.

This socket is the only transcription path — there is no batch fallback.
When it fails the recording is reported as failed, and the daemon's health
monitor (client.py) is what tells the user whether the server is there at all.
"""
from __future__ import annotations

import base64
import json
import queue
import threading
from typing import Callable

from .config import Config


class StreamError(Exception):
    """The realtime endpoint could not be reached, or the stream failed."""


def _open_socket(cfg: Config):
    try:
        from websockets.sync.client import connect
    except ModuleNotFoundError:
        raise StreamError(
            "the websockets package is not installed — "
            "`pip install --user 'websockets>=12'`, or `pipx inject vani websockets`"
        ) from None
    # A default Python user agent is 403'd by Cloudflare in front of the
    # tunnel; see client.py, which learned the same lesson the hard way.
    headers = {"User-Agent": "vani/2"}
    token = cfg.resolved_token()
    if token:
        headers["Authorization"] = "Bearer " + token
    return connect(cfg.server.url, max_size=None,
                   additional_headers=headers,
                   open_timeout=15, close_timeout=5)


class LiveStream:
    """One recording's worth of streaming transcription.

    `start()` returns immediately — connecting happens on the pump thread, so
    a slow handshake never stalls the microphone loop. `send()` never blocks
    and never raises. `finish()` is the only call that reports trouble,
    because it is the one place the caller can still tell the user.

    `on_delta` receives the accumulated text so far, from the reader thread.
    """

    def __init__(self, cfg: Config,
                 on_delta: Callable[[str], None] = lambda t: None,
                 open_socket: Callable[[Config], object] = _open_socket):
        self.cfg = cfg
        self.on_delta = on_delta
        self._open_socket = open_socket
        self._q: "queue.Queue[bytes | None]" = queue.Queue()
        self._done = threading.Event()
        self._parts: list[str] = []
        self._text: str | None = None
        self._error: str | None = None
        self._ws = None
        self._pump = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._pump.start()

    def send(self, pcm: bytes) -> None:
        if self._error is None:
            self._q.put(pcm)

    def finish(self, timeout: float) -> str:
        """Commit the utterance and return the final transcript.

        Raises StreamError if anything went wrong at any point — connecting,
        sending, or the final transcript not arriving in time.
        """
        self._q.put(None)
        if not self._done.wait(timeout):
            self._fail("no final transcript after %.0fs" % timeout)
        # Let the pump drain before dropping the socket under its sends.
        self._pump.join(timeout=5)
        self._close()
        if self._error is not None:
            raise StreamError(self._error)
        text = self._text if self._text is not None else "".join(self._parts)
        return text.strip()

    def abort(self) -> None:
        """The recording was discarded; drop the socket, nobody wants the text."""
        if self._error is None:
            self._error = "aborted"
        self._q.put(None)  # unblock the pump so its thread exits
        self._done.set()
        self._close()

    # -- internals ---------------------------------------------------------

    def _fail(self, why: str) -> None:
        if self._error is None:
            self._error = why
        self._done.set()

    def _close(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def _run(self) -> None:
        """Pump thread: connect, then drain the queue into the socket."""
        try:
            ws = self._open_socket(self.cfg)
        except StreamError as exc:
            self._fail(str(exc))
            return
        except Exception as exc:
            self._fail(f"{type(exc).__name__}: {exc}")
            return
        self._ws = ws
        threading.Thread(target=self._read, args=(ws,), daemon=True).start()
        try:
            ws.send(json.dumps({"type": "session.update",
                                "model": self.cfg.server.model}))
            # A commit *without* final is what starts the generation task.
            ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
            while True:
                chunk = self._q.get()
                if chunk is None:
                    break
                ws.send(json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(chunk).decode()}))
            ws.send(json.dumps({"type": "input_audio_buffer.commit",
                                "final": True}))
        except Exception as exc:
            self._fail(f"{type(exc).__name__}: {exc}")
        finally:
            # Covers an abort() that landed before the connection was even up:
            # the socket only exists now, so only this thread can drop it.
            if self._error is not None:
                self._close()

    def _read(self, ws) -> None:
        """Reader thread: collect deltas until done or the socket dies."""
        try:
            while not self._done.is_set():
                msg = json.loads(ws.recv(timeout=60))
                kind = msg.get("type")
                if kind == "transcription.delta":
                    delta = msg.get("delta", "")
                    if delta:
                        self._parts.append(delta)
                        try:
                            self.on_delta("".join(self._parts))
                        except Exception:
                            pass  # a broken notifier must not kill the stream
                elif kind == "transcription.done":
                    self._text = msg.get("text") or "".join(self._parts)
                    self._done.set()
                elif kind == "error":
                    self._fail(str(msg.get("error", msg))[:200])
        except Exception as exc:
            self._fail(f"{type(exc).__name__}: {exc}")
