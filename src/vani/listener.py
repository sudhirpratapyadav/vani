"""v2 phase 1: the ear.

Capture runs in a thread because the microphone is a blocking `arecord` pipe;
the socket runs on an asyncio loop because it is a WebSocket. A queue joins
them, and the gate sits in the middle deciding what crosses.

    mic thread ──chunks──> gate ──bytes──> queue ──> socket ──> transcript

No agent and no actions yet: this exists to produce a real corpus of a real
room, because addressivity cannot be tuned by reasoning about it. Run it for a
few days and the phase 2 numbers are simply there.
"""
from __future__ import annotations

import asyncio
import queue
import signal
import sys
import threading
import time
from typing import NoReturn

from . import audio, paths, state, transcript
from .config import Config
from .stream import StreamError, transcribe
from .vad import Event, Gate

CHUNK_SEC = 0.2  # matches stream.frame_ms; one capture chunk is one WS frame


def log(msg: str) -> None:
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


class Listener:
    def __init__(self, cfg: Config, *, verbose: bool = False):
        self.cfg = cfg
        self.verbose = verbose
        self.gate = Gate(cfg, self.on_gate_event)
        self.log_file = transcript.Transcript(retain_days=cfg.stream.retain_days)
        self.chunk_bytes = int(CHUNK_SEC * cfg.recording.sample_rate * audio.SAMPLE_WIDTH)
        self.stopping = threading.Event()
        #: Set while the gate is active, cleared on release.
        self._activation_started = 0.0
        self._upstream: "asyncio.Queue[bytes | None]" = asyncio.Queue()
        self._session_task: asyncio.Task | None = None

    # -- gate callbacks ----------------------------------------------------

    def on_gate_event(self, event: Event) -> None:
        if event.kind == "activated":
            self._activation_started = time.time()
            log(f"active (replayed {event.seconds:.1f}s pre-roll)")
            state.set_status(state.RECORDING)
        else:
            log(f"listening again after {event.seconds:.0f}s")
            state.set_status(state.IDLE)

    # -- the loop ----------------------------------------------------------

    def run(self) -> NoReturn:
        paths.ensure_dirs()
        state.write_pidfile(paths.listener_pidfile())
        state.set_status(state.IDLE)
        self._install_signals()
        log("vani listening: gate releases after %ss | %s"
            % (_num(self.cfg.stream.inactive_after_sec), self.cfg.stream.url))
        try:
            asyncio.run(self._main())
        except KeyboardInterrupt:
            pass
        finally:
            self._shutdown()
        sys.exit(0)

    async def _main(self) -> None:
        loop = asyncio.get_running_loop()
        raw: "queue.Queue[bytes | None]" = queue.Queue(maxsize=256)
        reader = threading.Thread(target=self._capture, args=(raw,), daemon=True)
        reader.start()

        while not self.stopping.is_set():
            chunk = await loop.run_in_executor(None, raw.get)
            if chunk is None:
                break
            await self._route(chunk)
        await self._close()

    # -- routing -----------------------------------------------------------
    #
    # Shared by the microphone loop and --test-wav, so a replay exercises the
    # same activation and commit path the daemon uses rather than a copy of it.

    async def _route(self, chunk: bytes) -> None:
        was_active = self.gate.active
        send = self.gate.feed(chunk)

        if self.gate.active and not was_active:
            self._upstream = asyncio.Queue()
            self._session_task = asyncio.create_task(self._session(self._upstream))
        if send and self._session_task is not None and not self._session_task.done():
            self._upstream.put_nowait(send)
        if was_active and not self.gate.active:
            await self._close()

    async def _close(self) -> None:
        """Commit the open utterance, if any, and wait for its transcript."""
        if self._session_task is None:
            return
        self._upstream.put_nowait(None)
        task, self._session_task = self._session_task, None
        await asyncio.gather(task, return_exceptions=True)

    async def _session(self, upstream: "asyncio.Queue[bytes | None]") -> None:
        started = time.time()

        def on_delta(delta: str) -> None:
            if self.verbose:
                print(delta, end="", flush=True)

        def on_done(text: str) -> None:
            if self.verbose:
                print()
            if not text:
                log("(nothing transcribed)")
                return
            self.log_file.append(transcript.Entry(
                text=text, at=started, duration=time.time() - started))
            log(f"heard: {text[:100]}")

        try:
            await transcribe(self.cfg, upstream, on_delta, on_done)
        except StreamError as exc:
            log(f"stream failed: {exc}")
        except Exception as exc:  # a socket error must not kill the ear
            log(f"stream error: {type(exc).__name__}: {exc}")

    def _capture(self, out: "queue.Queue[bytes | None]") -> None:
        """Blocking mic reader; reopens the pipe if it dies."""
        mic = audio.Microphone(self.cfg.recording.sample_rate, self.chunk_bytes)
        while not self.stopping.is_set():
            mic.open()
            try:
                for chunk in mic.chunks():
                    if self.stopping.is_set():
                        break
                    try:
                        out.put(chunk, timeout=1)
                    except queue.Full:
                        pass  # the socket is behind; drop rather than grow
            finally:
                mic.close()
            if not self.stopping.is_set():
                log("microphone closed, reopening in 3s")
                time.sleep(3)
        out.put(None)

    def _install_signals(self) -> None:
        def on_term(_sig, _frm):
            self.stopping.set()

        signal.signal(signal.SIGTERM, on_term)
        signal.signal(signal.SIGINT, on_term)

    def _shutdown(self) -> None:
        log("stopped listening")
        state.set_status(state.IDLE)
        state.clear_pidfile(paths.listener_pidfile())


def _num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def is_running() -> int | None:
    return state.read_pidfile(paths.listener_pidfile())


def run_test_wav(cfg: Config, path: str, *, verbose: bool = True) -> int:
    """Replay a WAV through the gate and the real socket, printing what happens.

    Everything the daemon does except the microphone: the same gate, the same
    activation and commit path, the same server. The quickest way to answer
    "is the ear working" without talking at a laptop.
    """
    try:
        pcm = audio.read_wav(path, cfg.recording.sample_rate)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    listener = Listener(cfg, verbose=verbose)
    seconds = audio.duration(pcm, cfg.recording.sample_rate)
    print(f"replaying {seconds:.1f}s from {path} -> {cfg.stream.url}")

    async def replay() -> None:
        for i in range(0, len(pcm), listener.chunk_bytes):
            await listener._route(pcm[i:i + listener.chunk_bytes])
            await asyncio.sleep(CHUNK_SEC)   # real-time pace, as the mic would
        await listener._close()

    asyncio.run(replay())
    entries = listener.log_file.read(limit=5)
    print(f"\ntranscript now holds {len(listener.log_file.read())} entr"
          f"{'y' if len(listener.log_file.read()) == 1 else 'ies'}:")
    for e in entries:
        print(f"  {e.stamp}  {e.text}")
    return 0
