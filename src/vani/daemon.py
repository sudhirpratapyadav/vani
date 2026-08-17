"""The long-running listener: microphone in, typed text out.

Wiring only — the interesting logic lives in session.py (state machine),
wake.py (spotting), hotkey.py (key events) and client.py (the API).

The daemon also answers SIGUSR1 as if the hotkey had been pressed, which is how
`vani toggle` drives it when it is running: one process owns the microphone,
so a push-to-talk press never fights the daemon for the device.
"""
from __future__ import annotations

import os
import queue
import signal
import sys
import time
from typing import NoReturn

from . import audio, hotkey, paths, state, wake
from .client import Client, TranscribeError
from .config import Config, ConfigError
from .hotkey import HotkeyWatcher
from .notify import NullNotifier, Notifier
from .output import OutputError, Typist
from .session import Event, Session

#: 0.125 s of 16 kHz mono audio — small enough for a responsive countdown.
CHUNK_SEC = 0.125


def log(msg: str) -> None:
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


class Daemon:
    def __init__(self, cfg: Config, *, dry_run: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.notifier = NullNotifier() if dry_run else Notifier(cfg.output.notify)
        self.client = Client(cfg)
        self.typist = Typist(cfg.output.typer, cfg.output.type_delay_ms)
        self.spotter = wake.NullSpotter() if dry_run else self._build_spotter()
        self.session = Session(cfg, self.spotter, self.handle_clip, self.handle_event)
        self.events: "queue.Queue[str]" = queue.Queue()
        self.chunk_bytes = int(CHUNK_SEC * cfg.recording.sample_rate * audio.SAMPLE_WIDTH)

    def _build_spotter(self):
        try:
            return wake.build(self.cfg, self.cfg.recording.sample_rate)
        except wake.WakeError as exc:
            log(f"wake word disabled: {exc}")
            self.notifier.show(f"Wake word disabled: {exc}", 6000)
            return wake.NullSpotter()

    # -- callbacks from the state machine ----------------------------------

    def handle_event(self, event: Event) -> None:
        if event.kind == "started":
            log(f"recording started ({event.detail})")
            state.set_status(state.RECORDING)
            self.notifier.show(
                "● Listening — pause %.0fs to send, or press the key"
                % self.cfg.recording.silence_sec, 60000, replace=True)
        elif event.kind == "countdown":
            state.set_countdown(event.seconds)
            self.notifier.show("Sending in %.1fs — speak to continue" % event.seconds,
                               60000, replace=True)
        elif event.kind == "resumed":
            log("speech resumed, countdown cancelled")
            state.set_status(state.RECORDING)
            self.notifier.show("● Listening...", 60000, replace=True)
        elif event.kind == "finished":
            log("finishing (%s): %.1fs audio, mic=%s"
                % (event.detail, event.seconds, audio.default_source()))
        elif event.kind == "discarded":
            log(f"discarded ({event.detail}): {event.seconds:.1f}s, nothing to send")
            state.set_status(state.IDLE)
            self.notifier.show("(nothing captured, cancelled)", 2500, replace=True)

    def handle_clip(self, pcm: bytes) -> None:
        """Transcribe a finished clip and type the result."""
        state.set_status(state.TRANSCRIBING)
        if self.cfg.recording.auto_gain:
            pcm, factor = audio.auto_gain(pcm)
            if factor != 1.0:
                log("auto-gain applied (x%.1f)" % factor)

        wav = audio.to_wav(pcm, self.cfg.recording.sample_rate)
        if self.cfg.output.save_last_wav:
            _save_last_wav(wav)

        self.notifier.show("Transcribing %.1fs..."
                           % audio.duration(pcm, self.cfg.recording.sample_rate),
                           60000, replace=True)
        try:
            text = self.client.transcribe(wav)
        except (TranscribeError, ConfigError) as exc:
            log(f"send failed: {exc}")
            self.notifier.show(f"Failed: {exc}", 6000, replace=True)
            state.set_status(state.IDLE)
            return

        if not text:
            log("server returned no text")
            self.notifier.show("(no speech detected)", 3000, replace=True)
            state.set_status(state.IDLE)
            return

        deliver(text, self.typist, self.notifier, self.cfg)
        state.set_status(state.IDLE)

    # -- main loop ---------------------------------------------------------

    def run(self) -> NoReturn:
        paths.ensure_dirs()
        state.write_pidfile(paths.daemon_pidfile())
        state.set_status(state.IDLE)
        self._install_signals()

        if self.cfg.hotkey.enabled:
            watcher = HotkeyWatcher(self.cfg.hotkey.keycode, self.events,
                                    self.cfg.hotkey.debounce_sec, on_error=log)
            watcher.start()

        log("vani up: wake=%s | key=%s | silence stop=%ss warn=%ss | typing via %s"
            % (self.spotter.describe,
               self.cfg.hotkey.keycode if self.cfg.hotkey.enabled else "disabled",
               _num(self.cfg.recording.silence_sec),
               _num(self.cfg.recording.silence_warn_sec),
               self.typist.backend))

        mic = audio.Microphone(self.cfg.recording.sample_rate, self.chunk_bytes)
        while True:
            mic.open()
            try:
                if not self._pump(mic):
                    log("microphone closed, reopening in 3s")
                    time.sleep(3)
            finally:
                mic.close()

    def _pump(self, mic: audio.Microphone) -> bool:
        """Feed the session until it asks for a restart. False = mic died."""
        for chunk in mic.chunks():
            restart = False
            while not self.events.empty():
                self.events.get_nowait()
                restart = self.session.on_hotkey() or restart
            if restart or self.session.feed(chunk):
                return True
        return False

    def _install_signals(self) -> None:
        def on_toggle(_sig, _frm):
            self.events.put("key")

        def on_term(_sig, _frm):
            log("shutting down")
            state.set_status(state.IDLE)
            state.clear_pidfile(paths.daemon_pidfile())
            hotkey.sweep_stale_watchers()
            sys.exit(0)

        signal.signal(signal.SIGUSR1, on_toggle)
        signal.signal(signal.SIGTERM, on_term)
        signal.signal(signal.SIGINT, on_term)


def deliver(text: str, typist: Typist, notifier: Notifier, cfg: Config) -> None:
    """Type the transcript, record it in history, and confirm on screen."""
    try:
        backend = typist.deliver(text)
    except OutputError as exc:
        log(f"could not type text: {exc}")
        notifier.show(f"Transcribed but not typed: {exc}", 6000, replace=True)
        backend = None
    if cfg.output.history:
        state.append_history(text)
    log("text: %s" % text[:100])
    if backend == "clipboard":
        notifier.show("📋 copied: " + text[:80], 4000, replace=True)
    elif backend:
        notifier.show("✓ " + text[:80], 2500, replace=True)


def _save_last_wav(wav: bytes) -> None:
    try:
        paths.cache_dir().mkdir(parents=True, exist_ok=True)
        paths.last_wav().write_bytes(wav)
    except OSError as exc:
        log(f"could not save last.wav: {exc}")


def _num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def is_running() -> int | None:
    """Pid of a live daemon, or None."""
    return state.read_pidfile(paths.daemon_pidfile())


def signal_toggle() -> bool:
    """Ask a running daemon to start/stop recording. False if none is running."""
    pid = is_running()
    if pid is None:
        return False
    try:
        os.kill(pid, signal.SIGUSR1)
    except OSError:
        return False
    return True


def run_test_wav(cfg: Config, path: str) -> int:
    """Replay a WAV through the state machine, printing transitions.

    The full pipeline minus microphone, network, and desktop — enough to check
    that a wake phrase, some speech, and a pause produce a clip of the right
    length.
    """
    spotter = wake.NullSpotter()
    try:
        spotter = wake.build(cfg, cfg.recording.sample_rate)
    except wake.WakeError as exc:
        print(f"note: wake word disabled ({exc})")

    clips: list[bytes] = []

    def on_event(event: Event) -> None:
        detail = f" ({event.detail})" if event.detail else ""
        extra = f" {event.seconds:.1f}s" if event.seconds else ""
        print(f"  {event.kind}{detail}{extra}")

    session = Session(cfg, spotter, clips.append, on_event)
    chunk_bytes = int(CHUNK_SEC * cfg.recording.sample_rate * audio.SAMPLE_WIDTH)
    try:
        pcm = audio.read_wav(path, cfg.recording.sample_rate)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"replaying {audio.duration(pcm, cfg.recording.sample_rate):.1f}s from {path}")
    if isinstance(spotter, wake.NullSpotter):
        # Nothing can say the wake word here, so simulate a key press at t=0 —
        # otherwise the file plays back with the machine sitting idle.
        print("  (no spotter: simulating a hotkey press at the start)")
        session.on_hotkey()
    for i in range(0, len(pcm), chunk_bytes):
        session.feed(pcm[i:i + chunk_bytes])
    print("end of file: recording=%s buffered=%.1fs clips=%d"
          % (session.recording, session.buffered_sec, len(clips)))
    for n, clip in enumerate(clips, 1):
        print("  clip %d: %.1fs, peak %d"
              % (n, audio.duration(clip, cfg.recording.sample_rate), audio.peak(clip)))
    return 0
