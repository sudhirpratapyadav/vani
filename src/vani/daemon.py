"""The long-running listener: microphone in, typed text out.

Wiring only — the interesting logic lives in session.py (state machine),
wake.py (spotting), hotkey.py (key events) and client.py (the API).

Presentation happens over the event bus (events.py): the daemon publishes
state, mic level, live text, countdowns, results, and errors; the UI process
(tray.py) subscribes and draws the pill. Desktop notifications are the
*fallback*, used only while nothing is subscribed — one surface owns an
attended session, and it is never the notification system. Earcons
(sounds.py) are played here rather than in the UI so the audio channel works
even headless, which is exactly when it matters most.

The daemon also answers SIGUSR1 as if the hotkey had been tapped, which is
how `vani toggle` drives it when it is running: one process owns the
microphone, so a push-to-talk press never fights the daemon for the device.

The watched key is a two-semantic control (see hotkey.py for the watchers):

    tap while idle        start a hands-free recording (silence auto-stop)
    hold while idle       push-to-talk — send the moment the key is released
    tap while recording   send now
    hold while recording  cancel (the buzz confirms it)
"""
from __future__ import annotations

import os
import queue
import signal
import sys
import threading
import time
from typing import NoReturn

from . import audio, hotkey, paths, sounds, state, wake
from .client import ServerError, check_health
from .config import Config
from .config import load as load_config
from .events import EventBus
from .notify import NullNotifier, Notifier
from .output import OutputError, Typist, focused_window
from .session import Event, Session
from .stream import LiveStream, StreamError

#: 0.125 s of 16 kHz mono audio — small enough for a responsive countdown,
#: and the natural rate of the level feed the waveform draws from.
CHUNK_SEC = 0.125

#: Toggles disabled mode (mic closed). USR1/USR2 are taken by toggle/cancel.
PAUSE_SIGNAL = signal.SIGRTMIN + 1

#: A finish that is still waiting on the server after this long says so
#: instead of looking hung.
SLOW_FINISH_SEC = 4.0


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
        self.sounds = sounds.Player(cfg.ui.sounds and not dry_run)
        self.typist = Typist(cfg.output.typer, cfg.output.type_delay_ms)
        self.spotter = wake.NullSpotter() if dry_run else self._build_spotter()
        self.session = Session(cfg, self.spotter, self.handle_clip,
                               self.handle_event, self.handle_chunk,
                               can_start=self._may_start,
                               on_discard_audio=self._keep_discarded_audio)
        self.events: "queue.Queue[tuple[str, float]]" = queue.Queue()
        #: The event bus; created in run() so tests can drive a Daemon
        #: without a socket. While it is None, publishes are no-ops and the
        #: notifier carries everything.
        self.bus: EventBus | None = None
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
        #: Key-gesture state: when the key went down, whether that press
        #: started the recording, and whether the hold thresholds have fired.
        self._key_down_at: float | None = None
        self._press_started = False
        self._ptt = False
        self._cancel_fired = False
        self.chunk_bytes = int(CHUNK_SEC * cfg.recording.sample_rate * audio.SAMPLE_WIDTH)

    def _build_spotter(self):
        try:
            return wake.build(self.cfg, self.cfg.recording.sample_rate)
        except wake.WakeError as exc:
            log(f"wake word disabled: {exc}")
            self.notifier.show(f"Wake word disabled: {exc}", 6000)
            return wake.NullSpotter()

    # -- presentation ------------------------------------------------------

    def _publish(self, ev: str, **fields) -> None:
        if self.bus is not None:
            self.bus.publish(ev, **fields)

    def _ui_attached(self) -> bool:
        """Is anything subscribed to the bus? Decides notification fallback."""
        return self.bus is not None and self.bus.has_subscribers

    def _snapshot(self) -> "list[dict]":
        """What a fresh subscriber needs to draw the current moment."""
        current, countdown = state.read_status()
        events: "list[dict]" = [
            {"ev": "state", "state": current, "countdown": countdown}]
        if self._server_ok is not None:
            events.append({"ev": "server", "ok": self._server_ok})
        if self._live_text:
            events.append({"ev": "live", "text": self._live_text})
        return events

    def _set_status(self, value: str, countdown: float = 0.0) -> None:
        """The status file (for `vani status` and crash fallback) plus the
        equivalent bus event, always together so the two never disagree."""
        if value == state.SILENCE:
            state.set_countdown(countdown)
        else:
            state.set_status(value)
        self._publish("state", state=value, countdown=countdown)

    # -- callbacks from the state machine ----------------------------------

    def handle_event(self, event: Event) -> None:
        if event.kind == "started":
            # Naming the mic here, not just at finish: "wrong microphone" is
            # invisible in every other line of the log.
            log(f"recording started ({event.detail}) mic="
                f"{self.cfg.recording.device or audio.default_source()}")
            state.set_live("")
            self._live_text = ""
            self._set_status(state.RECORDING)
            self._start_live()
            self.sounds.play("wake" if event.detail == "wake word" else "start")
            if not self._ui_attached():
                self.notifier.show(
                    "● Listening — pause %.0fs to finish, or press the key"
                    % self.cfg.recording.silence_sec, 60000, replace=True)
        elif event.kind == "countdown":
            self._set_status(state.SILENCE, event.seconds)
            if not self._ui_attached():
                # "Typing", not "sending": the audio streamed while they
                # spoke; this clock only decides when the recording ends.
                self.notifier.show("Typing in %.1fs — %s" % (
                    event.seconds, _tail(self._live_text) or "speak to continue"),
                    60000, replace=True)
        elif event.kind == "resumed":
            log("speech resumed, countdown cancelled")
            self._set_status(state.RECORDING)
            if not self._ui_attached():
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
            self._set_status(state.IDLE)
            self.sounds.play("trouble")
            cancelled = event.detail == "cancelled"
            self._publish("discarded",
                          reason="cancelled" if cancelled else "nothing captured",
                          seconds=round(event.seconds, 1))
            if not self._ui_attached():
                self.notifier.show("Cancelled — nothing typed" if cancelled
                                   else "(nothing captured, cancelled)",
                                   2500, replace=True)
        elif event.kind == "blocked":
            # Refused at the moment of intent — better than a recording that
            # is doomed to fail 20 s later. Re-probe right away so the next
            # attempt has a fresh verdict.
            log(f"refused to start ({event.detail}): server unreachable")
            self.sounds.play("trouble")
            self._publish("error", message="Server unreachable — "
                          "dictation is blocked (checking again now)",
                          retry=False, blocked=True)
            self._health_wake.set()
            if not self._ui_attached():
                self.notifier.show("✗ Server unreachable — dictation is blocked",
                                   5000, replace=True)

    def _may_start(self) -> bool:
        """The session's start gate: don't record into a known-dead server."""
        return self._server_ok is not False

    def _keep_discarded_audio(self, pcm: bytes) -> None:
        """Cancelled/unusable audio still lands in last.wav: `vani retry`
        can resurrect a mistaken cancel, and nothing said is ever lost."""
        if self.cfg.output.save_last_wav:
            state.save_last_wav(audio.to_wav(pcm, self.cfg.recording.sample_rate))

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
        # The waveform's food: one level per chunk, 8 Hz, only while recording.
        self._publish("level", rms=round(audio.rms(chunk)),
                      threshold=round(self.session.speech_threshold))

    def on_live_text(self, text: str) -> None:
        """Words arriving mid-recording, on the stream's reader thread."""
        self._live_text = text
        # Transcribed words are speech, whatever the level detector thinks —
        # in a rumbly room they are the only evidence that arms the silence
        # stop and keeps the clip from being discarded as empty.
        self.session.notice_speech()
        state.set_live(text)
        self._publish("live", text=text)
        if not self._ui_attached():
            self.notifier.show("● " + _tail(text), 60000, replace=True)

    def handle_clip(self, pcm: bytes) -> None:
        """Finish the stream for a completed recording and type the result."""
        self._set_status(state.TRANSCRIBING)
        live, self._live = self._live, None

        wav = audio.to_wav(pcm, self.cfg.recording.sample_rate)
        if self.cfg.output.save_last_wav and not state.save_last_wav(wav):
            log(f"could not save {paths.last_wav()}")

        text = ""
        if live is not None:
            if not self._ui_attached():
                self.notifier.show("Finishing...", 60000, replace=True)
            slow = threading.Timer(SLOW_FINISH_SEC, self._publish, ("slow",),
                                   {"seconds": SLOW_FINISH_SEC})
            slow.daemon = True
            slow.start()
            try:
                text = live.finish(self.cfg.server.timeout_sec)
            except StreamError as exc:
                slow.cancel()
                log(f"transcription failed: {exc}")
                self.sounds.play("trouble")
                self._publish("error",
                              message=f"Transcription failed: {exc}", retry=True)
                if not self._ui_attached():
                    self.notifier.show(
                        f"✗ Transcription failed: {exc}\n"
                        "The audio is saved — `vani retry` sends it again.",
                        8000, replace=True)
                state.set_live("")
                self._set_status(state.IDLE)
                # Re-probe right away so "the stream died" comes with an
                # up-to-date answer to "is the server down?".
                self._health_wake.set()
                return
            slow.cancel()

        if not text:
            log("server returned no text")
            self.sounds.play("trouble")
            # Include the mic: "no speech" with speech happening almost always
            # means the wrong device is being recorded.
            mic = self.cfg.recording.device or audio.default_source()
            self._publish("error",
                          message=f"No speech detected — mic: {mic}", retry=False)
            if not self._ui_attached():
                self.notifier.show(f"(no speech detected — mic: {mic})",
                                   5000, replace=True)
            state.set_live("")
            self._set_status(state.IDLE)
            return

        state.set_live(text)
        self._deliver(text)
        state.set_live("")
        self._set_status(state.IDLE)

    def _deliver(self, text: str) -> None:
        """Type the transcript, record it, confirm — pill first, banner only
        when nothing is subscribed. Success is quiet; failure is loud."""
        target = focused_window()
        try:
            backend = self.typist.deliver(text)
        except OutputError as exc:
            log(f"could not type text: {exc}")
            self.sounds.play("trouble")
            self._publish("error",
                          message=f"Transcribed but not typed: {exc}", retry=True)
            if not self._ui_attached():
                self.notifier.show(f"Transcribed but not typed: {exc}", 6000,
                                   replace=True)
            backend = None
        if self.cfg.output.history:
            state.append_history(text)
        log("text: %s" % text[:100])
        if backend is None:
            return
        self.sounds.play("done")
        self._publish("result", ok=True, text=text, backend=backend,
                      target=target)
        if not self._ui_attached():
            if backend == "clipboard":
                self.notifier.show("📋 copied: " + text[:80], 4000, replace=True)
            else:
                self.notifier.show("✓ " + text[:80], 2500, replace=True)

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
        self._publish("server", ok=ok, detail=detail)
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
        self.bus = EventBus(self._snapshot)
        self.bus.start()
        self._install_signals()
        threading.Thread(target=self._health_loop, daemon=True).start()

        watcher = None
        if self.cfg.hotkey.enabled:
            watcher = hotkey.build(self.cfg.hotkey, self.events, on_error=log)
            watcher.start()

        log("vani up: wake=%s | key=%s (%s) | silence stop=%ss warn=%ss | %s | typing via %s"
            % (self.spotter.describe,
               self.cfg.hotkey.keycode if self.cfg.hotkey.enabled else "disabled",
               watcher.backend if watcher else "-",
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
        self._set_status(state.DISABLED)
        if not self._ui_attached():
            self.notifier.show("Dictation disabled — not listening", 3000,
                               replace=True)
        while self.paused:
            time.sleep(0.2)
            # Key presses made while disabled must not fire on re-enable.
            while not self.events.empty():
                self.events.get_nowait()
            self.toggle_requested = False
            self.cancel_requested = False
            self._key_down_at = None
        log("dictation enabled")
        self._set_status(state.IDLE)
        if not self._ui_attached():
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
                kind, stamp = self.events.get_nowait()
                restart = self._handle_key(kind, stamp) or restart
            restart = self._tick_key() or restart
            if restart or self.session.feed(chunk):
                return True
        return False

    # -- key gestures ------------------------------------------------------

    def _handle_key(self, kind: str, stamp: float) -> bool:
        """One watcher event; returns True when the mic needs a restart."""
        if kind == "down":
            self._key_down_at = stamp
            self._cancel_fired = False
            if not self.session.recording:
                self._press_started = True
                self._ptt = False
                self.session.on_hotkey()
                if not self.session.recording:  # refused (blocked)
                    self._key_down_at = None
            else:
                self._press_started = False
            return False
        # "up"
        if self._key_down_at is None:
            return False
        held = max(0.0, stamp - self._key_down_at)
        self._key_down_at = None
        if not self.session.recording:
            return False
        if self._press_started:
            if self._ptt or held >= self.cfg.hotkey.hold_sec:
                # Push-to-talk: the release is the send.
                return self.session.on_hotkey()
            return False  # a tap: hands-free recording continues
        if held < self.cfg.hotkey.cancel_hold_sec:
            return self.session.on_hotkey()  # tap while recording: send now
        return False  # long hold released between thresholds: already handled

    def _tick_key(self) -> bool:
        """Time-based gesture decisions, checked once per audio chunk."""
        if self._key_down_at is None or not self.session.recording:
            return False
        held = time.time() - self._key_down_at
        if self._press_started:
            if not self._ptt and held >= self.cfg.hotkey.hold_sec:
                self._ptt = True
                self.session.hands_free = False
                log("push-to-talk (key held) — release to send")
                self._publish("mode", ptt=True)
            return False
        if not self._cancel_fired and held >= self.cfg.hotkey.cancel_hold_sec:
            self._cancel_fired = True
            self._key_down_at = None  # the eventual release is spent
            log("cancelled (key held during recording)")
            return self.session.cancel()
        return False

    # -- signals -----------------------------------------------------------

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

        def on_reload(_sig, _frm):
            # SIGHUP: re-read the sound switch, the one setting the tray
            # toggles live. Everything else still needs a restart.
            try:
                fresh = load_config(required=False)
            except Exception:
                return
            self.cfg.ui.sounds = fresh.ui.sounds
            self.sounds = sounds.Player(fresh.ui.sounds and not self.dry_run)

        def on_term(_sig, _frm):
            log("shutting down")
            state.set_status(state.IDLE)
            state.clear_pidfile(paths.daemon_pidfile())
            state.clear_pidfile(paths.server_file())  # stale verdicts help nobody
            if self.bus is not None:
                self.bus.close()
            hotkey.sweep_stale_watchers()
            sys.exit(0)

        signal.signal(signal.SIGUSR1, on_toggle)
        signal.signal(signal.SIGUSR2, on_cancel)
        signal.signal(PAUSE_SIGNAL, on_pause)
        signal.signal(signal.SIGHUP, on_reload)
        signal.signal(signal.SIGTERM, on_term)
        signal.signal(signal.SIGINT, on_term)


def deliver(text: str, typist: Typist, notifier: Notifier, cfg: Config) -> None:
    """Type the transcript, record it in history, and confirm on screen.

    The standalone paths (`vani toggle` without a daemon, `vani say --type`,
    `vani retry`) — the daemon itself delivers through Daemon._deliver, where
    the pill is the confirmation surface.
    """
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


def signal_reload() -> bool:
    """Ask a running daemon to re-read the live-reloadable settings."""
    return _signal(signal.SIGHUP)


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
