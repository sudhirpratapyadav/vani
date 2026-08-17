"""State-machine tests: no microphone, no network, no desktop.

Audio is synthesised, so the wake/record/silence behaviour that is otherwise
only testable by talking at a laptop becomes an ordinary unit test.
"""
from __future__ import annotations

import math
import unittest
from array import array

from vani import audio
from vani.config import Config
from vani.session import Session
from vani.wake import Spotter

RATE = 16000
CHUNK_SEC = 0.125
CHUNK_BYTES = int(RATE * CHUNK_SEC) * 2


def tone(seconds: float, amplitude: int = 12000, freq: float = 220.0) -> bytes:
    n = int(RATE * seconds)
    a = array("h", (int(amplitude * math.sin(2 * math.pi * freq * i / RATE))
                    for i in range(n)))
    return a.tobytes()


def silence(seconds: float, amplitude: int = 20) -> bytes:
    """Not digital silence — a quiet room, so the noise floor has something to track."""
    n = int(RATE * seconds)
    a = array("h", ((i % 3 - 1) * amplitude for i in range(n)))
    return a.tobytes()


class ScriptedSpotter(Spotter):
    """Fires the wake word after a set number of chunks."""

    def __init__(self, fire_after: int | None = None):
        self.fire_after = fire_after
        self.chunks = 0
        self.resets = 0

    def feed(self, chunk: bytes) -> bool:
        self.chunks += 1
        return self.fire_after is not None and self.chunks == self.fire_after

    def reset(self) -> None:
        self.resets += 1


class SessionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Config()
        self.cfg.recording.silence_sec = 1.0
        self.cfg.recording.silence_warn_sec = 0.5
        self.cfg.recording.max_sec = 5.0
        self.clips: list[bytes] = []
        self.events: list = []

    def make(self, spotter: Spotter | None = None) -> Session:
        return Session(self.cfg, spotter or ScriptedSpotter(),
                       self.clips.append, self.events.append)

    def feed(self, session: Session, pcm: bytes) -> list[bool]:
        return [session.feed(pcm[i:i + CHUNK_BYTES])
                for i in range(0, len(pcm), CHUNK_BYTES)]

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]

    # -- wake word ---------------------------------------------------------

    def test_wake_word_starts_recording(self):
        session = self.make(ScriptedSpotter(fire_after=2))
        self.feed(session, silence(0.5))
        self.assertTrue(session.recording)
        self.assertEqual(self.kinds()[0], "started")
        self.assertEqual(self.events[0].detail, "wake word")

    def test_idle_audio_is_not_buffered(self):
        session = self.make(ScriptedSpotter(fire_after=None))
        self.feed(session, tone(2.0))
        self.assertFalse(session.recording)
        self.assertEqual(session.buffered_sec, 0.0)
        self.assertEqual(self.clips, [])

    # -- the full cycle ----------------------------------------------------

    def test_speech_then_silence_sends_one_clip(self):
        session = self.make(ScriptedSpotter(fire_after=1))
        self.feed(session, silence(0.2) + tone(1.5) + silence(1.5))
        self.assertEqual(len(self.clips), 1)
        self.assertIn("countdown", self.kinds())
        self.assertIn("finished", self.kinds())
        self.assertFalse(session.recording)

    def test_trailing_silence_is_trimmed(self):
        self.cfg.recording.keep_tail_sec = 0.4
        session = self.make(ScriptedSpotter(fire_after=1))
        self.feed(session, silence(0.2) + tone(2.0) + silence(1.5))
        clip = audio.duration(self.clips[0], RATE)
        # ~2 s of speech plus the 0.4 s tail we deliberately keep.
        self.assertGreater(clip, 2.0)
        self.assertLess(clip, 2.7)

    def test_speaking_again_cancels_the_countdown(self):
        session = self.make(ScriptedSpotter(fire_after=1))
        self.feed(session, silence(0.2) + tone(1.0) + silence(0.7) + tone(1.0))
        self.assertIn("countdown", self.kinds())
        self.assertIn("resumed", self.kinds())
        self.assertTrue(session.recording)
        self.assertEqual(self.clips, [])

    def test_countdown_counts_down(self):
        session = self.make(ScriptedSpotter(fire_after=1))
        self.feed(session, silence(0.2) + tone(1.0) + silence(0.9))
        counts = [e.seconds for e in self.events if e.kind == "countdown"]
        self.assertGreater(len(counts), 1)
        self.assertEqual(counts, sorted(counts, reverse=True))

    def test_max_length_forces_a_send(self):
        self.cfg.recording.max_sec = 1.0
        session = self.make(ScriptedSpotter(fire_after=1))
        self.feed(session, silence(0.2) + tone(3.0))
        self.assertEqual(len(self.clips), 1)
        self.assertEqual(self.events[-1].detail, "max length")

    def test_silence_only_recording_is_discarded(self):
        session = self.make(ScriptedSpotter(fire_after=1))
        self.feed(session, silence(3.0))
        self.assertEqual(self.clips, [])

    # -- hotkey ------------------------------------------------------------

    def test_hotkey_starts_and_sends(self):
        session = self.make(ScriptedSpotter())
        session.on_hotkey()
        self.assertTrue(session.recording)
        self.feed(session, tone(1.0))
        restart = session.on_hotkey()
        self.assertTrue(restart)
        self.assertEqual(len(self.clips), 1)
        self.assertEqual(self.events[-1].detail, "key press")

    def test_hotkey_without_speech_discards(self):
        session = self.make(ScriptedSpotter())
        session.on_hotkey()
        self.feed(session, silence(0.6))
        session.on_hotkey()
        self.assertEqual(self.clips, [])
        self.assertEqual(self.events[-1].kind, "discarded")

    def test_finish_resets_the_spotter(self):
        spotter = ScriptedSpotter(fire_after=1)
        session = self.make(spotter)
        self.feed(session, silence(0.2) + tone(1.0) + silence(1.5))
        self.assertEqual(spotter.resets, 1)

    def test_a_second_recording_can_follow(self):
        spotter = ScriptedSpotter(fire_after=1)
        session = self.make(spotter)
        self.feed(session, silence(0.2) + tone(1.0) + silence(1.5))
        session.on_hotkey()
        self.feed(session, tone(1.0))
        session.on_hotkey()
        self.assertEqual(len(self.clips), 2)

    # -- adaptive threshold ------------------------------------------------

    def test_noise_floor_rises_in_a_loud_room(self):
        session = self.make(ScriptedSpotter())
        quiet = session.speech_threshold
        self.feed(session, silence(5.0, amplitude=900))
        self.assertGreater(session.speech_threshold, quiet)

    def test_threshold_has_a_floor(self):
        session = self.make(ScriptedSpotter())
        self.feed(session, silence(5.0, amplitude=1))
        self.assertGreaterEqual(session.speech_threshold, 350.0)


class AudioTest(unittest.TestCase):
    def test_peak_and_rms(self):
        pcm = tone(0.1, amplitude=10000)
        self.assertAlmostEqual(audio.peak(pcm), 10000, delta=50)
        self.assertAlmostEqual(audio.rms(pcm), 10000 / math.sqrt(2), delta=200)

    def test_auto_gain_boosts_quiet_audio(self):
        pcm = tone(0.5, amplitude=1000)
        louder, factor = audio.auto_gain(pcm)
        self.assertGreater(factor, 1.0)
        self.assertGreater(audio.peak(louder), audio.peak(pcm))

    def test_auto_gain_leaves_loud_audio_alone(self):
        pcm = tone(0.5, amplitude=20000)
        same, factor = audio.auto_gain(pcm)
        self.assertEqual(factor, 1.0)
        self.assertEqual(same, pcm)

    def test_auto_gain_ignores_silence(self):
        pcm = silence(0.5, amplitude=10)
        same, factor = audio.auto_gain(pcm)
        self.assertEqual(factor, 1.0)
        self.assertEqual(same, pcm)

    def test_amplify_clips_instead_of_wrapping(self):
        loud = audio.amplify(tone(0.1, amplitude=30000), 4.0)
        self.assertLessEqual(audio.peak(loud), 32768)

    def test_wav_roundtrip(self):
        import io
        import wave

        pcm = tone(0.25)
        with wave.open(io.BytesIO(audio.to_wav(pcm, RATE))) as w:
            self.assertEqual(w.getframerate(), RATE)
            self.assertEqual(w.getnchannels(), 1)
            self.assertEqual(w.readframes(w.getnframes()), pcm)


if __name__ == "__main__":
    unittest.main()
