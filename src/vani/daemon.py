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
import threading
import time
from typing import NoReturn

from . import audio, hotkey, paths, state, wake
from .client import ServerError, check_health
from .config import Config
from .hotkey import HotkeyWatcher
from .notify import NullNotifier, Notifier
from .output import OutputError, Typist
from .session import Event, Session
from .stream import LiveStream, StreamError

#: 0.125 s of 16 kHz mono audio — small enough for a responsive countdown.
CHUNK_SEC = 0.125

#: Toggles disabled mode (mic closed). USR1/USR2 are taken by toggle/cancel.
PAUSE_SIGNAL = signal.SIGRTMIN + 1


def log(msg: str) -> None:
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


class Daemon:
    def __init__(self, cfg: Config, *, dry_run: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.notifier = NullNotifier() if dry_run else Notifier(cfg.output.notify)
        #: A second notification slot, so a connectivity banner never eats the
        #: live transcript (both update their own message in place).
        self.health_notifier = NullNotifier() if dry_run else Notifier(cfg.output.notify)
        self.typist = Typist(cfg.output.typer, cfg.output.type_delay_ms)
        self.spotter = wake.NullSpotter() if dry_run else self._build_spotter()
        self.session = Session(cfg, self.spotter, self.handle_clip,
                               self.handle_event, self.handle_chunk)
        self.events: "queue.Queue[str]" = queue.Queue()
        #: The open realtime stream while recording.
        self._live: LiveStream | None = None
        self._live_text = ""
        #: Last health verdict; None until the first probe has run.
        self._server_ok: bool | None = None
        self._health_wake = threading.Event()
        #: Set by the SIGUSR1 handler; see _install_signals for why not a queue.
        self.toggle_requested = False
        #: Set by the SIGUSR2 handler: discard the recording, type nothing.
        self.cancel_requested = False
        #: Flipped by the pause signal. While True the microphone is closed —
        #: nothing is captured, not captured-and-ignored.
        self.paused = False
        #: Whether the overlay process was alive when recording started; live
        #: text and the countdown go to it instead of the notification then.
        self._ui_live = False
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
            # Naming the mic here, not just at finish: "wrong microphone" is
            # invisible in every other line of the log.
            log(f"recording started ({event.detail}) mic="
                f"{self.cfg.recording.device or audio.default_source()}")
            state.set_live("")
            state.set_status(state.RECORDING)
            self._ui_live = (self.cfg.ui.enabled and
                             state.read_pidfile(paths.tray_pidfile()) is not None)
            self._start_live()
            if not self._ui_live:
                self.notifier.show(
                    "● Listening — pause %.0fs to finish, or press the key"
                    % self.cfg.recording.silence_sec, 60000, replace=True)
        elif event.kind == "countdown":
            state.set_countdown(event.seconds)
            if not self._ui_live:
                # "Typing", not "sending": the audio streamed while they
                # spoke; this clock only decides when the recording ends.
                self.notifier.show("Typing in %.1fs — %s" % (
                    event.seconds, _tail(self._live_text) or "speak to continue"),
                    60000, replace=True)
        elif event.kind == "resumed":
            log("speech resumed, countdown cancelled")
            state.set_status(state.RECORDING)
            if not self._ui_live:
                self.notifier.show("● " + (_tail(self._live_text) or "Listening..."),
                                   60000, replace=True)
        elif event.kind == "finished":
            log("finishing (%s): %.1fs audio, mic=%s"
                % (event.detail, event.seconds, audio.default_source()))
        elif event.kind == "discarded":
            log(f"discarded ({event.detail}): {event.seconds:.1f}s, nothing to send")
            if self._live is not None:
                self._live.abort()
                self._live = None
            state.set_live("")
            state.set_status(state.IDLE)
            self.notifier.show("Cancelled — nothing typed"
                               if event.detail == "cancelled"
                               else "(nothing captured, cancelled)",
                               2500, replace=True)

    def _start_live(self) -> None:
        """Open the realtime stream for this recording."""
        self._live_text = ""
        self._live = None
        if not self.dry_run:
            self._live = LiveStream(self.cfg, self.on_live_text)
            self._live.start()

    def handle_chunk(self, chunk: bytes) -> None:
        if self._live is not None:
            self._live.send(chunk)

    def on_live_text(self, text: str) -> None:
        """Words arriving mid-recording, on the stream's reader thread."""
        self._live_text = text
        # Transcribed words are speech, whatever the level detector thinks —
        # in a rumbly room they are the only evidence that arms the silence
        # stop and keeps the clip from being discarded as empty.
        self.session.notice_speech()
        state.set_live(text)
        if not self._ui_live:
            self.notifier.show("● " + _tail(text), 60000, replace=True)

    def handle_clip(self, pcm: bytes) -> None:
        """Finish the stream for a completed recording and type the result."""
        state.set_status(state.TRANSCRIBING)
        live, self._live = self._live, None

        wav = audio.to_wav(pcm, self.cfg.recording.sample_rate)
        if self.cfg.output.save_last_wav and not state.save_last_wav(wav):
            log(f"could not save {paths.last_wav()}")

        text = ""
        if live is not None:
            self.notifier.show("Finishing...", 60000, replace=True)
            try:
                text = live.finish(self.cfg.server.timeout_sec)
            except StreamError as exc:
                log(f"transcription failed: {exc}")
                self.notifier.show(f"✗ Transcription failed: {exc}", 8000,
                                   replace=True)
                state.set_live("")
                state.set_status(state.IDLE)
                # Re-probe right away so "the stream died" comes with an
                # up-to-date answer to "is the server down?".
                self._health_wake.set()
                return

        if not text:
            log("server returned no text")
            # Include the mic: "no speech" with speech happening almost always
            # means the wrong device is being recorded.
            self.notifier.show("(no speech detected — mic: %s)"
                               % (self.cfg.recording.device or audio.default_source()),
                               5000, replace=True)
            state.set_live("")
            state.set_status(state.IDLE)
            return

        state.set_live(text)  # the overlay lingers on the final transcript
        deliver(text, self.typist, self.notifier, self.cfg)
        state.set_status(state.IDLE)

    # -- server health -----------------------------------------------------

    def _health_loop(self) -> None:
        """Probe the server now, then on an interval, then whenever woken.

        The user is informed on every change of verdict and left alone
        otherwise: one banner when the server goes away, one when it is back,
        never one per failed recording.
        """
        interval = self.cfg.server.health_check_min * 60
        while True:
            self._probe_server()
            if interval <= 0:
                return
            self._health_wake.wait(interval)
            self._health_wake.clear()

    def _probe_server(self) -> None:
        try:
            check_health(self.cfg)
            ok, detail = True, ""
        except ServerError as exc:
            ok, detail = False, str(exc)
        state.set_server(ok, detail)
        if ok == self._server_ok:
            return
        was, self._server_ok = self._server_ok, ok
        if ok:
            log(f"server online: {self.cfg.health_url}")
            if was is False:
                self.health_notifier.show("✓ Transcription server is back online",
                                          4000, replace=True)
        else:
            log(f"server unreachable: {detail}")
            self.health_notifier.show(
                "✗ Transcription server unreachable — dictation will not work.\n"
                + detail, 10000, replace=True)

    # -- main loop ---------------------------------------------------------

    def run(self) -> NoReturn:
        paths.ensure_dirs()
        state.write_pidfile(paths.daemon_pidfile())
        state.set_status(state.IDLE)
        self._install_signals()
        threading.Thread(target=self._health_loop, daemon=True).start()

        if self.cfg.hotkey.enabled:
            watcher = HotkeyWatcher(self.cfg.hotkey.keycode, self.events,
                                    self.cfg.hotkey.debounce_sec, on_error=log)
            watcher.start()

        log("vani up: wake=%s | key=%s | silence stop=%ss warn=%ss | %s | typing via %s"
            % (self.spotter.describe,
               self.cfg.hotkey.keycode if self.cfg.hotkey.enabled else "disabled",
               _num(self.cfg.recording.silence_sec),
               _num(self.cfg.recording.silence_warn_sec),
               self.cfg.server.url,
               self.typist.backend))

        mic = audio.Microphone(self.cfg.recording.sample_rate, self.chunk_bytes,
                               self.cfg.recording.device or None)
        while True:
            if self.paused:
                self._park()
                continue
            mic.open()
            try:
                if not self._pump(mic):
                    log("microphone closed, reopening in 3s")
                    time.sleep(3)
            finally:
                mic.close()

    def _park(self) -> None:
        """Disabled: sit with the microphone closed until re-enabled."""
        log("dictation disabled — microphone closed")
        state.set_status(state.DISABLED)
        self.notifier.show("Dictation disabled — not listening", 3000, replace=True)
        while self.paused:
            time.sleep(0.2)
            # Key presses made while disabled must not fire on re-enable.
            while not self.events.empty():
                self.events.get_nowait()
            self.toggle_requested = False
            self.cancel_requested = False
        log("dictation enabled")
        state.set_status(state.IDLE)
        self.notifier.show("Dictation enabled", 2500, replace=True)

    def _pump(self, mic: audio.Microphone) -> bool:
        """Feed the session until it asks for a restart. False = mic died."""
        for chunk in mic.chunks():
            if self.paused:
                # Cancel whatever is open and hand back to run(), which
                # closes the microphone and parks.
                self.session.cancel()
                return True
            restart = False
            if self.cancel_requested:
                self.cancel_requested = False
                restart = self.session.cancel()
            if self.toggle_requested:
                # Cleared first: a signal arriving during on_hotkey sets the
                # flag again and is handled on the next chunk rather than lost.
                self.toggle_requested = False
                restart = self.session.on_hotkey() or restart
            while not self.events.empty():
                self.events.get_nowait()
                restart = self.session.on_hotkey() or restart
            if restart or self.session.feed(chunk):
                return True
        return False

    def _install_signals(self) -> None:
        def on_toggle(_sig, _frm):
            # Only an attribute assignment, which is atomic. Putting to the
            # queue here can deadlock: the handler runs on the main thread, and
            # if the signal lands while that thread is inside empty()/get_nowait()
            # it would block forever on a lock it already holds itself.
            self.toggle_requested = True

        def on_cancel(_sig, _frm):
            self.cancel_requested = True

        def on_pause(_sig, _frm):
            self.paused = not self.paused

        def on_term(_sig, _frm):
            log("shutting down")
            state.set_status(state.IDLE)
            state.clear_pidfile(paths.daemon_pidfile())
            state.clear_pidfile(paths.server_file())  # stale verdicts help nobody
            hotkey.sweep_stale_watchers()
            sys.exit(0)

        signal.signal(signal.SIGUSR1, on_toggle)
        signal.signal(signal.SIGUSR2, on_cancel)
        signal.signal(PAUSE_SIGNAL, on_pause)
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


def _num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _tail(text: str, limit: int = 160) -> str:
    """The end of the live transcript — the words just spoken, not the start."""
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def is_running() -> int | None:
    """Pid of a live daemon, or None."""
    return state.read_pidfile(paths.daemon_pidfile())


def signal_toggle() -> bool:
    """Ask a running daemon to start/stop recording. False if none is running."""
    return _signal(signal.SIGUSR1)


def signal_cancel() -> bool:
    """Ask a running daemon to discard the current recording."""
    return _signal(signal.SIGUSR2)


def signal_pause_toggle() -> bool:
    """Flip a running daemon between enabled and disabled."""
    return _signal(PAUSE_SIGNAL)


def _signal(sig: int) -> bool:
    pid = is_running()
    if pid is None:
        return False
    try:
        os.kill(pid, sig)
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
