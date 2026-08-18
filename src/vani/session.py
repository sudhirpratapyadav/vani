"""The wake / record / silence state machine.

This is the core of the daemon and it deliberately knows nothing about
microphones, HTTP, or notifications: it takes audio chunks and a hotkey signal,
and hands a finished clip to a callback. That keeps it exercisable offline —
see `vani start --test-wav` and tests/test_session.py.

Silence detection adapts to the room. While idle, an exponential average of the
input level tracks the ambient noise floor, and speech is anything a few times
louder than that. A fixed threshold works at a desk and fails on a train.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from . import audio
from .config import Config

#: Ceiling on what the idle average will accept as "noise floor".
NOISE_CEILING = 4000.0
#: Smoothing for the noise-floor average (per chunk).
NOISE_ALPHA = 0.03
#: Countdown updates below this delta are not worth a redraw.
COUNTDOWN_STEP = 0.3
#: Fraction of the loudest recent speech that still counts as speech, used when
#: the device is quieter than `recording.min_speech_level` assumes.
QUIET_DEVICE_FRACTION = 0.25
#: However quiet the input, never drop the bar below this — otherwise a stream
#: carrying nothing but noise would read as continuous speech and never stop.
ABSOLUTE_MIN_SPEECH_LEVEL = 40.0
#: Per-chunk decay of the loudest-speech envelope, so one cough near the start
#: cannot hold the threshold high for the rest of the recording.
SPEECH_PEAK_DECAY = 0.995


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
        on_chunk: Callable[[bytes], None] = lambda c: None,
    ):
        self.cfg = cfg
        self.spotter = spotter
        self.on_clip = on_clip
        self.on_event = on_event
        #: Called with every chunk that enters the buffer, in order, so a live
        #: consumer sees exactly the audio the finished clip will contain.
        self.on_chunk = on_chunk

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
        #: Decaying maximum of recent chunk levels; calibrates the threshold to
        #: however loud this microphone actually is. See speech_threshold.
        self.speech_peak = 0.0

    @property
    def buffered_sec(self) -> float:
        return self._buflen / self.bytes_per_sec

    @property
    def speech_threshold(self) -> float:
        """The level a chunk must reach to count as speech.

        Two independent guards, whichever is higher: a multiple of the ambient
        noise floor (handles a loud room) and an absolute floor (handles a dead
        silent one). The absolute floor assumes a normally scaled microphone,
        which is not a safe assumption — a Bluetooth headset in HFP mode
        delivers speech peaking near RMS 400, well under the default 350 once
        the quieter syllables are counted, so every pause between words looked
        like silence and recordings were cut off mid-sentence. So the floor
        also scales down to a fraction of the loudest speech actually heard.
        """
        rec = self.cfg.recording
        floor = rec.min_speech_level
        if self.speech_peak > 0:  # only once we know how loud this mic runs
            floor = min(floor, max(self.speech_peak * QUIET_DEVICE_FRACTION,
                                   ABSOLUTE_MIN_SPEECH_LEVEL))
        return max(self.noise_floor * rec.speech_factor, floor)

    # -- inputs ------------------------------------------------------------

    def on_hotkey(self) -> bool:
        """Key pressed: start recording, or send what is buffered."""
        if self.recording:
            return self._finish("key press")
        self._start("key press")
        return False

    def notice_speech(self) -> None:
        """External proof that someone is talking — the streaming ASR
        returned words for this recording.

        The level detector can be deaf in a room it mishears: a C920 webcam
        measured ambient RMS ~1650 (85% of it sub-300 Hz fan rumble), which
        drove the adaptive threshold above actual speech, so no chunk ever
        counted as speech and the silence stop never armed. Transcribed words
        are evidence no threshold can argue with, so they feed the same
        had-speech/silence bookkeeping as a loud chunk.

        Called from the stream's reader thread while feed() runs on the main
        thread: attribute writes only, safe under the GIL.
        """
        if not self.recording:
            return
        self._had_speech = True
        if self._silence_bytes:
            self._silence_bytes = 0
            if self._countdown_shown >= 0:
                self._countdown_shown = -1.0
                self._emit("resumed")

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
        self.on_chunk(chunk)
        # Updated before the comparison so the first loud chunk raises the bar
        # for the quiet ones that follow it, not the other way round.
        self.speech_peak = max(level, self.speech_peak * SPEECH_PEAK_DECAY)

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
