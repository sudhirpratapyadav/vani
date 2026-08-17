"""The voice activity gate: is anyone talking at all right now?

v1 asks a fine-grained question — "has this clip ended yet" — every 125 ms.
v2 asks a much coarser one, on a timescale of minutes: over a day a person
speaks maybe four hours out of twenty-four, and the other twenty are silence
nobody should pay to stream, transcribe, or store.

Three properties matter more than the detector behind them:

- **It does not micro-gate.** Once active, every chunk goes upstream — pauses
  between sentences included. Cutting those out would hand the model
  discontinuous audio and destroy the boundaries it punctuates with.
- **The thresholds are asymmetric.** Activation is immediate; release waits
  `stream.inactive_after_sec`, so thinking mid-sentence never flips it.
- **Activation replays a pre-roll buffer.** By the time speech crosses the
  threshold its first syllable has already gone past, so the last second of
  audio is kept at all times and sent ahead of the chunk that triggered it.
  Without this the gate eats the first word of every session.

Like `session.py`, this knows nothing about sockets or microphones: it takes
chunks and returns the bytes to send. That is what lets a whole day be
replayed from a WAV file in the tests, with no mic, no network, and no GPU.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Callable

from . import audio
from .config import Config
from .session import (
    ABSOLUTE_MIN_SPEECH_LEVEL,
    NOISE_ALPHA,
    NOISE_CEILING,
    QUIET_DEVICE_FRACTION,
    SPEECH_PEAK_DECAY,
)

#: Nobody is talking; audio stays on this machine.
LISTENING = "listening"
#: Someone is talking; audio is going upstream.
ACTIVE = "active"


@dataclass
class Event:
    """A state change. The daemon logs these; the tests assert on them."""

    kind: str        # activated | deactivated
    detail: str = ""
    seconds: float = 0.0


class Gate:
    def __init__(self, cfg: Config, on_event: Callable[[Event], None] = lambda e: None):
        self.cfg = cfg
        self.on_event = on_event
        self.bytes_per_sec = cfg.recording.sample_rate * audio.SAMPLE_WIDTH
        self.state = LISTENING
        self.noise_floor = 150.0
        #: Decaying maximum of recent levels; calibrates the bar to this mic.
        self.speech_peak = 0.0
        self._silence_bytes = 0
        self._active_bytes = 0
        self._preroll: deque[bytes] = deque()
        self._preroll_bytes = 0
        self._preroll_limit = int(cfg.stream.preroll_sec * self.bytes_per_sec)

    # -- state -------------------------------------------------------------

    @property
    def active(self) -> bool:
        return self.state == ACTIVE

    @property
    def silence_sec(self) -> float:
        """Quiet so far in the current active stretch."""
        return self._silence_bytes / self.bytes_per_sec

    @property
    def active_sec(self) -> float:
        return self._active_bytes / self.bytes_per_sec

    @property
    def speech_threshold(self) -> float:
        """The level a chunk must reach to count as speech.

        Same shape as `session.speech_threshold`, and for the same reason: the
        absolute floor assumes a normally scaled microphone, which a Bluetooth
        headset in HFP mode is not. See session.py for the measurements.
        """
        rec = self.cfg.recording
        floor = rec.min_speech_level
        if self.speech_peak > 0:
            floor = min(floor, max(self.speech_peak * QUIET_DEVICE_FRACTION,
                                   ABSOLUTE_MIN_SPEECH_LEVEL))
        return max(self.noise_floor * rec.speech_factor, floor)

    # -- input -------------------------------------------------------------

    def feed(self, chunk: bytes) -> bytes:
        """Process one chunk; return the bytes that should go upstream.

        Empty while listening. On activation the return value is the pre-roll
        buffer followed by this chunk, so the caller can stay oblivious to the
        distinction and simply send whatever it gets back.
        """
        level = audio.rms(chunk)

        if self.state == LISTENING:
            self._track_noise(level)
            if level >= self.speech_threshold:
                return self._activate(chunk, level)
            self._remember(chunk)
            return b""

        # Active: everything goes, whether or not this particular chunk is
        # speech. Only the release timer cares about the difference.
        self.speech_peak = max(level, self.speech_peak * SPEECH_PEAK_DECAY)
        self._active_bytes += len(chunk)
        if level >= self.speech_threshold:
            self._silence_bytes = 0
        else:
            self._silence_bytes += len(chunk)
            if self.silence_sec >= self.cfg.stream.inactive_after_sec:
                self._deactivate("%ss quiet" % _num(self.cfg.stream.inactive_after_sec))
        return chunk

    # -- transitions -------------------------------------------------------

    def _track_noise(self, level: float) -> None:
        self.noise_floor = ((1 - NOISE_ALPHA) * self.noise_floor
                            + NOISE_ALPHA * min(level, NOISE_CEILING))

    def _remember(self, chunk: bytes) -> None:
        """Hold the most recent `preroll_sec` of audio, and no more."""
        self._preroll.append(chunk)
        self._preroll_bytes += len(chunk)
        while self._preroll_bytes > self._preroll_limit and self._preroll:
            self._preroll_bytes -= len(self._preroll.popleft())

    def release(self, detail: str = "") -> None:
        """Drop back to listening from outside — used when the socket dies.

        Without this a failed stream leaves the gate active and every chunk
        goes nowhere until the release timer expires, so someone can talk for
        half a minute into a closed socket with one line in the log.
        """
        if self.state == ACTIVE:
            self._deactivate(detail)

    def _activate(self, chunk: bytes, level: float) -> bytes:
        preroll = b"".join(self._preroll)
        self._preroll.clear()
        self._preroll_bytes = 0
        self.state = ACTIVE
        self.speech_peak = level
        self._silence_bytes = 0
        self._active_bytes = len(preroll) + len(chunk)
        self.on_event(Event("activated", seconds=len(preroll) / self.bytes_per_sec))
        return preroll + chunk

    def _deactivate(self, detail: str = "") -> None:
        seconds = self.active_sec
        self.state = LISTENING
        self.speech_peak = 0.0
        self._silence_bytes = 0
        self._active_bytes = 0
        self.on_event(Event("deactivated", detail, seconds))


def _num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)
