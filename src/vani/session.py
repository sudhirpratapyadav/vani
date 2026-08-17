"""The wake / record / silence state machine.

This is the core of the daemon and it deliberately knows nothing about
microphones, HTTP, or notifications: it takes audio chunks and a hotkey signal,
and hands a finished clip to a callback. That keeps it exercisable offline —
see `vani daemon --test-wav` and tests/test_session.py.

Silence detection adapts to the room. While idle, an exponential average of the
input level tracks the ambient noise floor, and speech is anything a few times
louder than that. A fixed threshold works at a desk and fails on a train.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from . import audio
from .config import Config

#: Multiple of the ambient noise floor that counts as speech.
SPEECH_FACTOR = 3.5
#: Never treat anything below this as speech, however quiet the room is.
MIN_SPEECH_LEVEL = 350.0
#: Ceiling on what the idle average will accept as "noise floor".
NOISE_CEILING = 4000.0
#: Smoothing for the noise-floor average (per chunk).
NOISE_ALPHA = 0.03
#: Countdown updates below this delta are not worth a redraw.
COUNTDOWN_STEP = 0.3


@dataclass
class Event:
    """Something the state machine did; the daemon logs these, tests assert on them."""

    kind: str        # started | countdown | resumed | finished | discarded
    detail: str = ""
    seconds: float = 0.0


class Session:
    def __init__(
        self,
        cfg: Config,
        spotter,
        on_clip: Callable[[bytes], None],
        on_event: Callable[[Event], None] = lambda e: None,
    ):
        self.cfg = cfg
        self.spotter = spotter
        self.on_clip = on_clip
        self.on_event = on_event

        rec = cfg.recording
        self.rate = rec.sample_rate
        self.bytes_per_sec = self.rate * audio.SAMPLE_WIDTH
        self.max_bytes = int(rec.max_sec * self.bytes_per_sec)
        self.min_bytes = int(rec.min_sec * self.bytes_per_sec)
        self.keep_tail_bytes = int(rec.keep_tail_sec * self.bytes_per_sec)

        self.noise_floor = 150.0
        self.recording = False
        self._reset_buffer()

    # -- state -------------------------------------------------------------

    def _reset_buffer(self) -> None:
        self._buf: list[bytes] = []
        self._buflen = 0
        self._had_speech = False
        self._silence_bytes = 0
        self._countdown_shown = -1.0

    @property
    def buffered_sec(self) -> float:
        return self._buflen / self.bytes_per_sec

    @property
    def speech_threshold(self) -> float:
        return max(self.noise_floor * SPEECH_FACTOR, MIN_SPEECH_LEVEL)

    # -- inputs ------------------------------------------------------------

    def on_hotkey(self) -> bool:
        """Key pressed: start recording, or send what is buffered."""
        if self.recording:
            return self._finish("key press")
        self._start("key press")
        return False

    def feed(self, chunk: bytes) -> bool:
        """Process one audio chunk.

        Returns True when the caller should restart the microphone — the clip
        has been handed off and whatever accumulated in the pipe meanwhile is
        stale audio that would bleed into the next recording.
        """
        level = audio.rms(chunk)
        if not self.recording:
            self._track_noise(level)
            if self.spotter.feed(chunk):
                self._start("wake word")
            return False

        self._buf.append(chunk)
        self._buflen += len(chunk)

        if level >= self.speech_threshold:
            self._had_speech = True
            if self._silence_bytes:
                self._silence_bytes = 0
                if self._countdown_shown >= 0:
                    self._countdown_shown = -1.0
                    self._emit("resumed")
        else:
            self._silence_bytes += len(chunk)

        silence_sec = self._silence_bytes / self.bytes_per_sec
        if self._had_speech and silence_sec >= self.cfg.recording.silence_warn_sec:
            remaining = max(0.0, self.cfg.recording.silence_sec - silence_sec)
            if abs(remaining - self._countdown_shown) >= COUNTDOWN_STEP:
                self._countdown_shown = remaining
                self._emit("countdown", seconds=remaining)
            if silence_sec >= self.cfg.recording.silence_sec:
                return self._finish("%.0fs silence" % self.cfg.recording.silence_sec)

        if self._buflen >= self.max_bytes:
            return self._finish("max length")
        return False

    # -- transitions -------------------------------------------------------

    def _track_noise(self, level: float) -> None:
        self.noise_floor = ((1 - NOISE_ALPHA) * self.noise_floor
                            + NOISE_ALPHA * min(level, NOISE_CEILING))

    def _start(self, why: str) -> None:
        self.recording = True
        self._reset_buffer()
        self._emit("started", why)

    def _finish(self, why: str) -> bool:
        pcm = b"".join(self._buf)
        # Trim the trailing silence that triggered the stop, keeping a short
        # tail so the last word isn't cut mid-syllable.
        if self._silence_bytes > self.keep_tail_bytes:
            pcm = pcm[: len(pcm) - (self._silence_bytes - self.keep_tail_bytes)]

        usable = len(pcm) >= self.min_bytes and self._had_speech
        seconds = audio.duration(pcm, self.rate)

        self.recording = False
        self._reset_buffer()
        self.spotter.reset()  # a fresh recognizer; the old one heard the whole clip

        if not usable:
            self._emit("discarded", why, seconds=seconds)
        else:
            self._emit("finished", why, seconds=seconds)
            self.on_clip(pcm)
        return True

    def _emit(self, kind: str, detail: str = "", seconds: float = 0.0) -> None:
        self.on_event(Event(kind, detail, seconds))
