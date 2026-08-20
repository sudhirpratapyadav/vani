"""Live transcription over a realtime ASR WebSocket.

Chunks go up while the user is still talking and words come back a fraction of
a second behind the speech, so the desktop can show text forming before the
recording has even stopped.

Two services speak two different dialects, so the wire format lives in a small
protocol object and `LiveStream` stays the same machinery for both:

**Deepgram** (`wss://api.deepgram.com/v1/listen`, the default) takes raw PCM
frames and answers with `Results` messages — interim ones that revise
themselves, then an `is_final` one per phrase:

    -> <raw linear16 bytes>                         ... repeatedly
    -> {"type": "CloseStream"}                      flush and finish
    <- {"type": "Results", "is_final": false, ...}  a guess, may change
    <- {"type": "Results", "is_final": true, ...}   a settled phrase
    <- {"type": "Metadata", ...}                    the stream is done

**Voxtral** (an OpenAI-realtime-shaped vLLM server) takes base64 in JSON and
answers with deltas:

    -> session.update            {"model": ...}
    -> input_audio_buffer.commit                 starts the generation task
    -> input_audio_buffer.append {"audio": <base64 PCM16>}   ... repeatedly
    -> input_audio_buffer.commit {"final": true}             ends the utterance
    <- transcription.delta       {"delta": " word"}
    <- transcription.done        {"text": "the whole thing"}

The daemon is deliberately synchronous, so this is built on the sync
`websockets` client with two small threads per recording — a pump that drains
a queue into the socket, and a reader that collects results — rather than
dragging asyncio into the main loop.

This socket is the only transcription path — there is no batch fallback.
When it fails the recording is reported as failed, and the daemon's health
monitor (client.py) is what tells the user whether the service is there at all.
"""
from __future__ import annotations

import base64
import json
import queue
import threading
import time
from typing import Callable
from urllib.parse import urlencode

from .config import Config


class StreamError(Exception):
    """The realtime endpoint could not be reached, or the stream failed."""


# --------------------------------------------------------------------------
# Protocols


class _Voxtral:
    """OpenAI-realtime-shaped vLLM: base64 audio in JSON, deltas back."""

    name = "voxtral"

    def open_frames(self, cfg: Config) -> list:
        return [
            json.dumps({"type": "session.update", "model": cfg.server.model}),
            # A commit *without* final is what starts the generation task.
            json.dumps({"type": "input_audio_buffer.commit"}),
        ]

    def audio_frame(self, chunk: bytes):
        return json.dumps({"type": "input_audio_buffer.append",
                           "audio": base64.b64encode(chunk).decode()})

    def close_frames(self) -> list:
        return [json.dumps({"type": "input_audio_buffer.commit", "final": True})]

    def handle(self, msg: dict, stream: "LiveStream") -> None:
        kind = msg.get("type")
        if kind == "transcription.delta":
            delta = msg.get("delta", "")
            if delta:
                self._parts.append(delta)
                stream._push("".join(self._parts))
        elif kind == "transcription.done":
            stream._complete(msg.get("text") or "".join(self._parts))
        elif kind == "error":
            stream._fail(str(msg.get("error", msg))[:200])

    def __init__(self) -> None:
        self._parts: list[str] = []


class _Deepgram:
    """Deepgram realtime: raw PCM up, interim and final Results back.

    Interim results revise themselves as the model hears more, so the live
    text is the settled phrases plus whatever the current guess is — never
    the interim appended to itself.
    """

    name = "deepgram"

    def __init__(self) -> None:
        self._finals: list[str] = []
        self._interim = ""

    def open_frames(self, cfg: Config) -> list:
        return []  # everything is in the query string

    def audio_frame(self, chunk: bytes):
        return chunk  # raw linear16, no envelope

    def close_frames(self) -> list:
        # Asks the server to flush its buffer and send remaining finals,
        # rather than dropping the socket and losing the tail.
        return [json.dumps({"type": "CloseStream"})]

    def _text(self) -> str:
        return " ".join(self._finals + ([self._interim] if self._interim else []))

    def handle(self, msg: dict, stream: "LiveStream") -> None:
        kind = msg.get("type")
        if kind == "Results":
            try:
                said = msg["channel"]["alternatives"][0].get("transcript", "")
            except (KeyError, IndexError, TypeError):
                return
            if msg.get("is_final"):
                # An empty final is just a silent window; it settles nothing.
                if said:
                    self._finals.append(said)
                self._interim = ""
                if said:
                    stream._push(self._text())
            elif said != self._interim:
                self._interim = said
                stream._push(self._text())
        elif kind == "Metadata":
            # Sent once the server has flushed everything it is going to send.
            stream._complete(" ".join(self._finals))
        elif kind in ("Error", "error"):
            stream._fail(str(msg.get("description")
                             or msg.get("message")
                             or msg.get("error", msg))[:200])


def protocol_for(cfg: Config):
    return _Deepgram() if cfg.provider == "deepgram" else _Voxtral()


def socket_url(cfg: Config) -> str:
    """The URL to connect to, with the query string a provider needs."""
    url = cfg.server.url
    if cfg.provider != "deepgram" or "?" in url:
        return url  # a hand-written query string is left exactly as given
    return url + "?" + urlencode({
        "model": cfg.server.model,
        "encoding": "linear16",
        "sample_rate": cfg.recording.sample_rate,
        "channels": 1,
        # The live caption is the whole point of streaming, so interim
        # results are not optional here.
        "interim_results": "true",
        "punctuate": "true",
        "smart_format": "true",
    })


def socket_headers(cfg: Config) -> dict:
    # A default Python user agent is 403'd by Cloudflare in front of the
    # tunnel; see client.py, which learned the same lesson the hard way.
    headers = {"User-Agent": "vani/2"}
    token = cfg.resolved_token()
    if cfg.provider == "deepgram":
        if not token:
            raise StreamError(
                "no Deepgram API key — put it in server.token, or export "
                "DEEPGRAM_API_KEY")
        headers["Authorization"] = "Token " + token
    elif token:
        headers["Authorization"] = "Bearer " + token
    return headers


def _open_socket(cfg: Config):
    try:
        from websockets.sync.client import connect
    except ModuleNotFoundError:
        raise StreamError(
            "the websockets package is not installed — "
            "`pip install --user 'websockets>=12'`, or `pipx inject vani websockets`"
        ) from None
    return connect(socket_url(cfg), max_size=None,
                   additional_headers=socket_headers(cfg),
                   open_timeout=15, close_timeout=5)


# --------------------------------------------------------------------------
# The stream


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
        self.proto = protocol_for(cfg)
        self._open_socket = open_socket
        self._q: "queue.Queue[bytes | None]" = queue.Queue()
        self._done = threading.Event()
        #: When the server last said anything; finish() waits on activity,
        #: not a stopwatch, so a long utterance may drain for minutes.
        self._last_event = time.monotonic()
        self._accum = ""
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

        The audio is long since sent; what remains is the model draining its
        backlog, which for a long utterance takes longer than any fixed wait.
        So `timeout` bounds server *inactivity*, not the total: as long as
        results keep arriving, finish keeps waiting. And a transcript in hand
        always beats an error — if the final event never comes, the text
        collected so far is returned rather than thrown away. StreamError is
        raised only when there is genuinely no text to type.
        """
        self._q.put(None)
        while not self._done.wait(0.25):
            if time.monotonic() - self._last_event > timeout:
                break
        # Let the pump drain before dropping the socket under its sends.
        self._pump.join(timeout=5)
        self._close()
        text = (self._text if self._text is not None else self._accum).strip()
        if self._done.is_set() and self._error is None:
            return text  # may be empty: silence is a valid transcript
        if text:
            return text  # partial — the final event never arrived
        if self._error is not None:
            raise StreamError(self._error)
        raise StreamError("no transcript (%.0fs without server activity)" % timeout)

    def abort(self) -> None:
        """The recording was discarded; drop the socket, nobody wants the text."""
        if self._error is None:
            self._error = "aborted"
        self._q.put(None)  # unblock the pump so its thread exits
        self._done.set()
        self._close()

    # -- called by the protocol --------------------------------------------

    def _push(self, text: str) -> None:
        """The transcript so far, as the protocol currently understands it."""
        self._accum = text
        try:
            self.on_delta(text)
        except Exception:
            pass  # a broken notifier must not kill the stream

    def _complete(self, text: str) -> None:
        self._text = text if text else self._accum
        self._done.set()

    def _fail(self, why: str) -> None:
        if self._error is None:
            self._error = why
        self._done.set()

    # -- internals ---------------------------------------------------------

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
            for frame in self.proto.open_frames(self.cfg):
                ws.send(frame)
            while True:
                chunk = self._q.get()
                if chunk is None:
                    break
                ws.send(self.proto.audio_frame(chunk))
            for frame in self.proto.close_frames():
                ws.send(frame)
        except Exception as exc:
            self._fail(f"{type(exc).__name__}: {exc}")
        finally:
            # Covers an abort() that landed before the connection was even up:
            # the socket only exists now, so only this thread can drop it.
            if self._error is not None:
                self._close()

    def _read(self, ws) -> None:
        """Reader thread: hand every message to the protocol until done."""
        try:
            while not self._done.is_set():
                raw = ws.recv(timeout=60)
                self._last_event = time.monotonic()
                if isinstance(raw, bytes):
                    continue  # neither service sends binary back
                self.proto.handle(json.loads(raw), self)
        except Exception as exc:
            self._fail(f"{type(exc).__name__}: {exc}")
