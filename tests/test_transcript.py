"""The rolling transcript: append, expire, survive damage."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from vani.transcript import Entry, Transcript

DAY = 86400.0


class TranscriptTest(unittest.TestCase):
    def make(self, retain_days: float = 14.0) -> Transcript:
        path = Path(tempfile.mkdtemp()) / "transcript.jsonl"
        return Transcript(path, retain_days=retain_days)

    def test_append_and_read(self):
        log = self.make()
        log.append(Entry("hello there", at=time.time()))
        log.append(Entry("and again", at=time.time()))
        self.assertEqual([e.text for e in log.read()], ["hello there", "and again"])

    def test_reading_an_absent_file_is_empty(self):
        self.assertEqual(self.make().read(), [])

    def test_unicode_survives(self):
        log = self.make()
        log.append(Entry("hindi: नमस्ते — em dash", at=time.time()))
        self.assertEqual(log.read()[0].text, "hindi: नमस्ते — em dash")

    def test_expired_entries_are_not_returned(self):
        log = self.make(retain_days=7)
        now = time.time()
        log.append(Entry("ancient", at=now - 30 * DAY))
        log.append(Entry("recent", at=now))
        self.assertEqual([e.text for e in log.read()], ["recent"])

    def test_sweep_rewrites_the_file(self):
        log = self.make(retain_days=7)
        now = time.time()
        log._last_sweep = now          # isolate sweep() from the startup sweep
        for i in range(5):
            log.append(Entry(f"old {i}", at=now - 30 * DAY))
        log.append(Entry("keep", at=now))
        self.assertEqual(log.sweep(), 5)
        self.assertEqual(log.path.read_text().count("\n"), 1)
        self.assertEqual([e.text for e in log.read()], ["keep"])

    def test_the_first_append_sweeps(self):
        """A daemon down for a month must clean up when it comes back, not an
        hour later."""
        log = self.make(retain_days=7)
        stale = "\n".join(Entry(f"old {i}", at=time.time() - 30 * DAY).to_json()
                          for i in range(4))
        log.path.parent.mkdir(parents=True, exist_ok=True)
        log.path.write_text(stale + "\n")
        log.append(Entry("fresh", at=time.time()))
        self.assertEqual([e.text for e in log.read()], ["fresh"])
        self.assertEqual(log.path.read_text().count("\n"), 1)

    def test_sweep_is_a_noop_when_nothing_expired(self):
        log = self.make()
        log.append(Entry("fresh", at=time.time()))
        self.assertEqual(log.sweep(), 0)

    def test_a_torn_line_does_not_lose_the_file(self):
        """A killed process can leave half a line; the rest must still read."""
        log = self.make()
        log.append(Entry("before", at=time.time()))
        with log.path.open("a") as f:
            f.write('{"at": 123, "te\n')          # truncated mid-write
        log.append(Entry("after", at=time.time()))
        self.assertEqual([e.text for e in log.read()], ["before", "after"])

    def test_since_filters_by_time(self):
        log = self.make()
        now = time.time()
        log.append(Entry("first", at=now - 100))
        log.append(Entry("second", at=now))
        self.assertEqual([e.text for e in log.read(since=now - 50)], ["second"])

    def test_limit_returns_the_newest(self):
        log = self.make()
        now = time.time()
        for i in range(10):
            log.append(Entry(f"line {i}", at=now + i))
        self.assertEqual([e.text for e in log.read(limit=2)], ["line 8", "line 9"])

    def test_suspect_entries_round_trip(self):
        """Text the local gate scored as silence is kept, but marked."""
        log = self.make()
        log.append(Entry("probably a hallucination", at=time.time(), suspect=True))
        self.assertTrue(log.read()[0].suspect)

    def test_an_unwritable_path_does_not_raise(self):
        log = Transcript(Path("/proc/nonexistent/transcript.jsonl"))
        log.append(Entry("dropped", at=time.time()))   # must not raise
        self.assertEqual(log.read(), [])


if __name__ == "__main__":
    unittest.main()
