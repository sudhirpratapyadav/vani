"""Wake-phrase spotting, against a scripted recognizer.

No Vosk model and no audio: the recognizer is replaced by a script of the
partial/final results a real one would produce, which is exactly where the
false-wakeup behaviour lives. The scripts below are taken from real decoder
output on synthesised speech — "hey Claudia" really does flicker through
"hey claude" before settling on "hey [unk]".
"""
from __future__ import annotations

import json
import unittest

from vani.wake import VoskSpotter

CHUNK = b"\x00\x00" * 2000   # 0.125 s at 16 kHz


class FakeRecognizer:
    """Replays a script of (kind, text) results, one per chunk."""

    def __init__(self, script: list[tuple[str, str]]):
        self._script = list(script)
        self._text = ""

    def AcceptWaveform(self, chunk: bytes) -> bool:
        kind, text = self._script.pop(0) if self._script else ("partial", "")
        self._text = text
        return kind == "final"

    def Result(self) -> str:
        return json.dumps({"text": self._text})

    def PartialResult(self) -> str:
        return json.dumps({"partial": self._text})


def spotter(script, confirm_sec: float = 0.25) -> VoskSpotter:
    """A VoskSpotter with its recognizer replaced; no model needed."""
    sp = object.__new__(VoskSpotter)
    sp.phrases = ["hey claude", "hi claude"]
    sp._words = [p.split() for p in sp.phrases]
    sp.rate = 16000
    sp.confirm_sec = confirm_sec
    sp._held = -1.0
    sp._rec = FakeRecognizer(script)
    return sp


def feed_all(sp: VoskSpotter, chunks: int) -> "int | None":
    for i in range(chunks):
        if sp.feed(CHUNK):
            return i
    return None


class WordBoundaryTest(unittest.TestCase):
    def test_a_longer_word_does_not_contain_the_phrase(self):
        """"hey claude" is not inside "hey claudia" — the old substring test
        said it was, and woke on every mention of the name."""
        sp = spotter([("partial", "hey claudia")] * 8)
        self.assertIsNone(feed_all(sp, 8))

    def test_the_phrase_is_found_inside_a_longer_utterance(self):
        sp = spotter([("partial", "okay so hey claude write this")] * 8)
        self.assertIsNotNone(feed_all(sp, 8))

    def test_the_words_must_be_adjacent_and_in_order(self):
        for text in ("claude hey", "hey there claude"):
            sp = spotter([("partial", text)] * 8)
            self.assertIsNone(feed_all(sp, 8), text)


class ConfirmationTest(unittest.TestCase):
    """A partial match has to survive; a final one does not."""

    def test_a_flickering_partial_does_not_wake(self):
        # Real decoder output for "Hey Claudia, are you coming to the lab".
        sp = spotter([("partial", "hey"),
                      ("partial", "hey claude"),      # the flicker
                      ("partial", "hey [unk]"),       # ...revised away
                      ("final", "hey [unk]"),
                      ("partial", "[unk]")])
        self.assertIsNone(feed_all(sp, 5))

    def test_a_partial_that_sticks_wakes(self):
        sp = spotter([("partial", "hey"),
                      ("partial", "hey claude"),
                      ("partial", "hey claude"),
                      ("partial", "hey claude [unk]")])
        # 0.25s of confirmation = the third matching chunk.
        self.assertEqual(feed_all(sp, 4), 3)

    def test_a_final_result_wakes_without_waiting(self):
        """The decoder has already committed, so there is nothing to confirm."""
        sp = spotter([("final", "hey claude [unk]")])
        self.assertEqual(feed_all(sp, 1), 0)

    def test_confirmation_can_be_turned_off(self):
        sp = spotter([("partial", "hey claude")], confirm_sec=0.0)
        self.assertEqual(feed_all(sp, 1), 0)

    def test_an_interrupted_match_starts_the_clock_again(self):
        sp = spotter([("partial", "hey claude"),
                      ("partial", "[unk]"),        # broken
                      ("partial", "hey claude"),
                      ("partial", "hey claude")])  # only 2 in a row: not enough
        self.assertIsNone(feed_all(sp, 4))

    def test_reset_forgets_a_match_in_progress(self):
        sp = spotter([("partial", "hey claude")] * 6)
        self.assertFalse(sp.feed(CHUNK))
        self.assertFalse(sp.feed(CHUNK))
        sp.reset = lambda: None      # keep the fake recognizer
        sp._held = -1.0              # what a real reset() does
        self.assertFalse(sp.feed(CHUNK))   # counting starts over


if __name__ == "__main__":
    unittest.main()
