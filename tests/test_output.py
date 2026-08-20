"""Delivering text, and the Enter that optionally follows it."""
from __future__ import annotations

import unittest
from unittest import mock

from vani.output import OutputError, Typist


class FakeRunner:
    """Captures the commands a Typist would run."""

    def __init__(self, fail: bool = False):
        self.calls: list[list[str]] = []
        self.fail = fail

    def __call__(self, cmd: list[str]) -> None:
        self.calls.append(cmd)
        if self.fail:
            raise OutputError("nothing accepted the keystrokes")


def typist(backend: str, submit: bool) -> tuple[Typist, FakeRunner]:
    t = Typist(backend, delay_ms=0, submit=submit)
    runner = FakeRunner()
    t._run = runner
    return t, runner


class SubmitTest(unittest.TestCase):
    def test_off_by_default_nothing_extra_is_pressed(self):
        t, runner = typist("xdotool", submit=False)
        t.deliver("hello")
        self.assertEqual(len(runner.calls), 1)
        self.assertNotIn("Return", runner.calls[0])

    def test_enter_follows_the_text_as_a_separate_keypress(self):
        """Not part of the typed string: xdotool type would enter a literal
        newline, which a chat box reads as a line break, not a send."""
        t, runner = typist("xdotool", submit=True)
        t.deliver("send this")
        self.assertEqual(len(runner.calls), 2)
        self.assertIn("send this", runner.calls[0])
        self.assertEqual(runner.calls[1],
                         ["xdotool", "key", "--clearmodifiers", "Return"])

    def test_ydotool_presses_the_enter_keycode(self):
        t, runner = typist("ydotool", submit=True)
        t.deliver("send this")
        self.assertEqual(runner.calls[1], ["ydotool", "key", "28:1", "28:0"])

    def test_the_clipboard_has_nothing_to_submit(self):
        t, runner = typist("clipboard", submit=True)
        self.assertFalse(t.submits)

    def test_stdout_has_nothing_to_submit(self):
        t, _ = typist("stdout", submit=True)
        self.assertFalse(t.submits)

    def test_a_key_backend_reports_that_it_submits(self):
        for backend in ("xdotool", "ydotool"):
            self.assertTrue(typist(backend, submit=True)[0].submits)
            self.assertFalse(typist(backend, submit=False)[0].submits)

    def test_empty_text_presses_nothing(self):
        """A silent recording must not fire a stray Enter into the window."""
        t, runner = typist("xdotool", submit=True)
        t.deliver("")
        self.assertEqual(runner.calls, [])


if __name__ == "__main__":
    unittest.main()
