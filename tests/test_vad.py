"""Voice activity gate: a day replayed from synthesised audio.

No microphone, no network, no GPU — the gate takes chunks and returns bytes,
so the interesting behaviour (hysteresis, pre-roll, no micro-gating) is an
ordinary unit test.
"""
from __future__ import annotations

import unittest

from test_session import CHUNK_BYTES, RATE, silence, tone
from vani import vad
from vani.config import Config
from vani.vad import ACTIVE, LISTENING, Gate

#: Amplitude 600 -> RMS ~424: a Bluetooth headset at normal speaking volume.
QUIET = 600


class GateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Config()
        self.cfg.stream.inactive_after_sec = 5.0
        self.cfg.stream.preroll_sec = 1.0
        self.events: list = []

    def make(self) -> Gate:
        return Gate(self.cfg, self.events.append)

    def feed(self, gate: Gate, pcm: bytes) -> bytes:
        """Push audio through in chunks; return everything sent upstream."""
        return b"".join(gate.feed(pcm[i:i + CHUNK_BYTES])
                        for i in range(0, len(pcm), CHUNK_BYTES))

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]

    # -- listening ---------------------------------------------------------

    def test_silence_sends_nothing(self):
        gate = self.make()
        sent = self.feed(gate, silence(30.0))
        self.assertEqual(sent, b"")
        self.assertEqual(gate.state, LISTENING)
        self.assertEqual(self.events, [])

    def test_a_quiet_room_never_activates(self):
        """The 20 hours a day nobody is talking must cost nothing."""
        gate = self.make()
        self.feed(gate, silence(120.0, amplitude=40))
        self.assertEqual(gate.state, LISTENING)

    # -- activation --------------------------------------------------------

    def test_speech_activates_and_streams(self):
        gate = self.make()
        sent = self.feed(gate, silence(2.0) + tone(2.0))
        self.assertEqual(gate.state, ACTIVE)
        self.assertEqual(self.kinds(), ["activated"])
        self.assertGreater(len(sent), 0)

    def test_activation_replays_the_preroll(self):
        """Without this the gate eats the first word of every session."""
        gate = self.make()
        sent = self.feed(gate, silence(5.0) + tone(1.0))
        # 1 s of speech, but a second of pre-roll went with it.
        self.assertGreater(len(sent) / (RATE * 2), 1.8)
        self.assertAlmostEqual(self.events[0].seconds, 1.0, delta=0.2)

    def test_preroll_is_bounded(self):
        """Ten minutes of listening must not accumulate ten minutes of audio."""
        gate = self.make()
        self.feed(gate, silence(60.0))
        self.assertLessEqual(gate._preroll_bytes, gate._preroll_limit)

    def test_preroll_does_not_duplicate_audio(self):
        gate = self.make()
        sent = self.feed(gate, silence(0.5) + tone(1.0))
        # 0.5 s pre-roll + 1 s speech, not 0.5 + 1 + a replayed chunk.
        self.assertLess(len(sent) / (RATE * 2), 1.7)

    # -- no micro-gating ---------------------------------------------------

    def test_pauses_between_sentences_still_stream(self):
        """Chopping these out would destroy the model's sentence boundaries."""
        gate = self.make()
        sent = self.feed(gate, silence(0.5) + tone(1.0) + silence(2.0) + tone(1.0))
        streamed = len(sent) / (RATE * 2)
        self.assertGreater(streamed, 4.0, "the pause was cut out of the stream")
        self.assertEqual(gate.state, ACTIVE)
        self.assertEqual(self.kinds(), ["activated"])

    def test_a_short_pause_does_not_deactivate(self):
        gate = self.make()
        self.feed(gate, silence(0.5) + tone(1.0) + silence(4.0))
        self.assertEqual(gate.state, ACTIVE)
        self.assertNotIn("deactivated", self.kinds())

    # -- release -----------------------------------------------------------

    def test_long_silence_deactivates(self):
        gate = self.make()
        self.feed(gate, silence(0.5) + tone(1.0) + silence(6.0))
        self.assertEqual(gate.state, LISTENING)
        self.assertEqual(self.kinds(), ["activated", "deactivated"])

    def test_silence_after_release_sends_nothing_more(self):
        gate = self.make()
        self.feed(gate, silence(0.5) + tone(1.0) + silence(6.0))
        sent = self.feed(gate, silence(30.0))
        self.assertEqual(sent, b"", "still streaming after going inactive")

    def test_a_second_burst_reactivates(self):
        gate = self.make()
        self.feed(gate, silence(0.5) + tone(1.0) + silence(6.0)
                  + silence(2.0) + tone(1.0))
        self.assertEqual(gate.state, ACTIVE)
        self.assertEqual(self.kinds(), ["activated", "deactivated", "activated"])

    # -- quiet microphones -------------------------------------------------

    def test_a_quiet_headset_activates(self):
        """HFP speech peaks near RMS 430 — it must still cross the bar."""
        gate = self.make()
        self.feed(gate, silence(10.0, amplitude=3))
        self.feed(gate, tone(1.0, amplitude=QUIET))
        self.assertEqual(gate.state, ACTIVE)

    def test_a_quiet_headset_still_releases(self):
        gate = self.make()
        self.feed(gate, silence(10.0, amplitude=3) + tone(1.0, amplitude=QUIET)
                  + silence(6.0, amplitude=3))
        self.assertEqual(gate.state, LISTENING)

    # -- the whole point ---------------------------------------------------

    def test_a_sparse_day_streams_only_what_was_spoken(self):
        """Four bursts in ten minutes: upstream should be a small fraction."""
        gate = self.make()
        day = b""
        for _ in range(4):
            day += silence(120.0) + tone(3.0)
        sent = self.feed(gate, day + silence(10.0))
        heard = len(day) / (RATE * 2)
        streamed = len(sent) / (RATE * 2)
        self.assertLess(streamed / heard, 0.15,
                        f"streamed {streamed:.0f}s of {heard:.0f}s heard")
        self.assertGreater(streamed, 12.0, "lost actual speech")


class ThresholdTest(unittest.TestCase):
    def test_constants_are_shared_with_the_v1_state_machine(self):
        """One tuning story, not two — session.py is where it is explained."""
        from vani import session

        self.assertIs(vad.QUIET_DEVICE_FRACTION, session.QUIET_DEVICE_FRACTION)
        self.assertIs(vad.SPEECH_PEAK_DECAY, session.SPEECH_PEAK_DECAY)
        self.assertIs(vad.NOISE_ALPHA, session.NOISE_ALPHA)


if __name__ == "__main__":
    unittest.main()
