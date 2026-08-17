"""Daemon signal handling and the push-to-talk pidfile logic.

No microphone and no network: the daemon is built in dry-run mode (null
spotter, null notifier) and fed from a fake pipe.
"""
from __future__ import annotations

import os
import signal
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vani import daemon, paths, state, toggle
from vani.config import Config

SILENT_CHUNK = b"\x00\x00" * 2000


class FakeMic:
    """Stands in for audio.Microphone: yields a fixed list of chunks."""

    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks

    def chunks(self):
        return iter(self._chunks)


class SignalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.saved = {s: signal.getsignal(s)
                      for s in (signal.SIGUSR1, signal.SIGTERM, signal.SIGINT)}

    def tearDown(self) -> None:
        for sig, handler in self.saved.items():
            signal.signal(sig, handler)

    def test_sigusr1_only_sets_a_flag(self):
        """It must not touch the queue: the handler runs on the main thread,
        which may already hold the queue's non-reentrant lock."""
        d = daemon.Daemon(Config(), dry_run=True)
        d._install_signals()
        os.kill(os.getpid(), signal.SIGUSR1)
        self.assertTrue(d.toggle_requested)
        self.assertTrue(d.events.empty())

    def test_the_flag_starts_and_stops_a_recording(self):
        d = daemon.Daemon(Config(), dry_run=True)
        d._install_signals()

        os.kill(os.getpid(), signal.SIGUSR1)
        d._pump(FakeMic([SILENT_CHUNK]))
        self.assertFalse(d.toggle_requested)
        self.assertTrue(d.session.recording)

        os.kill(os.getpid(), signal.SIGUSR1)
        self.assertTrue(d._pump(FakeMic([SILENT_CHUNK])))  # asks for a mic restart
        self.assertFalse(d.session.recording)


class TogglePidfileTest(unittest.TestCase):
    """`vani toggle` when no daemon is running."""

    def setUp(self) -> None:
        home = Path(tempfile.mkdtemp())
        self.env = mock.patch.dict(os.environ, {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_RUNTIME_DIR": str(home / "run"),
        })
        self.env.start()
        paths.ensure_dirs()

    def tearDown(self) -> None:
        self.env.stop()

    def test_nothing_pending_on_a_clean_slate(self):
        self.assertFalse(toggle._pending_clip())

    def test_a_live_recorder_is_pending(self):
        state.write_pidfile(paths.toggle_pidfile())  # this process, so alive
        self.assertTrue(toggle._pending_clip())

    def test_a_clip_left_by_the_max_length_limit_is_pending(self):
        # arecord exits by itself at max_sec: the pid is dead but the clip is
        # good, and the next press must send it rather than start over.
        state.write_pidfile(paths.toggle_pidfile(), 999_999)
        paths.toggle_wav().write_bytes(b"RIFF")
        self.assertTrue(toggle._pending_clip())

    def test_a_dead_recorder_with_no_clip_is_not_pending(self):
        state.write_pidfile(paths.toggle_pidfile(), 999_999)
        self.assertFalse(toggle._pending_clip())

    def test_save_last_wav_survives_an_unwritable_cache(self):
        with mock.patch.object(Path, "write_bytes", side_effect=OSError("full")):
            self.assertFalse(state.save_last_wav(b"RIFF"))


if __name__ == "__main__":
    unittest.main()
