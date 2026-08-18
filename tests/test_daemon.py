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


class FakeLive:
    """Stands in for stream.LiveStream in the daemon's handle_clip path."""

    def __init__(self, text: str | None = None):
        self.text = text          # None = the stream failed
        self.finished = False
        self.aborted = False

    def finish(self, timeout: float) -> str:
        self.finished = True
        if self.text is None:
            raise daemon.StreamError("scripted failure")
        return self.text

    def abort(self) -> None:
        self.aborted = True


class LiveClipTest(unittest.TestCase):
    def setUp(self) -> None:
        home = Path(tempfile.mkdtemp())
        self.env = mock.patch.dict(os.environ, {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_RUNTIME_DIR": str(home / "run"),
        })
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()

    def make(self) -> daemon.Daemon:
        cfg = Config()
        cfg.output.typer = "stdout"
        cfg.output.history = False
        cfg.output.save_last_wav = False
        return daemon.Daemon(cfg, dry_run=True)

    def test_a_finished_stream_delivers_its_text(self):
        d = self.make()
        d._live = FakeLive("typed live")
        with mock.patch.object(daemon, "deliver") as deliver:
            d.handle_clip(SILENT_CHUNK)
        deliver.assert_called_once()
        self.assertEqual(deliver.call_args[0][0], "typed live")
        self.assertIsNone(d._live)  # consumed; the next clip starts fresh

    def test_a_dead_stream_tells_the_user_and_rechecks_the_server(self):
        d = self.make()
        d._live = FakeLive(text=None)
        with mock.patch.object(daemon, "deliver") as deliver:
            d.handle_clip(SILENT_CHUNK)
        deliver.assert_not_called()  # nothing must be typed on failure
        self.assertIn("✗ Transcription failed: scripted failure",
                      d.notifier.messages)
        # A failed recording wakes the health monitor for a fresh verdict.
        self.assertTrue(d._health_wake.is_set())

    def test_a_discarded_recording_aborts_the_stream(self):
        d = self.make()
        live = FakeLive("nobody wants this")
        d._live = live
        d.handle_event(daemon.Event("discarded", "too short"))
        self.assertTrue(live.aborted)
        self.assertIsNone(d._live)
        self.assertFalse(live.finished)

    def test_sigusr2_cancels_the_recording(self):
        saved = signal.getsignal(signal.SIGUSR2)
        self.addCleanup(signal.signal, signal.SIGUSR2, saved)
        d = self.make()
        d._install_signals()
        d.session.on_hotkey()
        self.assertTrue(d.session.recording)
        os.kill(os.getpid(), signal.SIGUSR2)
        self.assertTrue(d.cancel_requested)
        self.assertTrue(d._pump(FakeMic([SILENT_CHUNK])))  # mic restart
        self.assertFalse(d.session.recording)

    def test_live_notifications_pause_while_the_overlay_is_alive(self):
        d = self.make()
        state.write_pidfile(paths.tray_pidfile())  # this process: alive
        d.session.on_hotkey()          # emits "started" -> checks the pidfile
        self.assertTrue(d._ui_live)
        before = len(d.notifier.messages)
        d.on_live_text("hello overlay")
        self.assertEqual(len(d.notifier.messages), before)  # no banner
        self.assertEqual(state.read_live(), "hello overlay")  # file instead

    def test_live_notifications_return_without_the_overlay(self):
        d = self.make()
        d.session.on_hotkey()
        self.assertFalse(d._ui_live)
        d.on_live_text("hello notification")
        self.assertIn("● hello notification", d.notifier.messages)

    def test_pause_signal_cancels_the_recording_and_asks_for_a_restart(self):
        saved = signal.getsignal(daemon.PAUSE_SIGNAL)
        self.addCleanup(signal.signal, daemon.PAUSE_SIGNAL, saved)
        d = self.make()
        d._install_signals()
        d.session.on_hotkey()
        os.kill(os.getpid(), daemon.PAUSE_SIGNAL)
        self.assertTrue(d.paused)
        # The pump hands control back so run() can close the mic and park.
        self.assertTrue(d._pump(FakeMic([SILENT_CHUNK])))
        self.assertFalse(d.session.recording)
        os.kill(os.getpid(), daemon.PAUSE_SIGNAL)  # toggle back
        self.assertFalse(d.paused)

    def test_disabled_status_roundtrips(self):
        state.set_status(state.DISABLED)
        self.assertEqual(state.read_status(), (state.DISABLED, 0.0))

    def test_live_text_marks_the_session_as_having_heard_speech(self):
        d = self.make()
        d.session.on_hotkey()
        self.assertFalse(d.session._had_speech)
        d.on_live_text("hello there")
        self.assertTrue(d.session._had_speech)

    def test_health_probe_notifies_on_change_only(self):
        d = self.make()
        with mock.patch.object(daemon, "check_health",
                               side_effect=daemon.ServerError("502")):
            d._probe_server()
            d._probe_server()  # same verdict — must stay silent
        self.assertEqual(len(d.health_notifier.messages), 1)
        self.assertIn("unreachable", d.health_notifier.messages[0])
        with mock.patch.object(daemon, "check_health", return_value=None):
            d._probe_server()
        self.assertIn("back online", d.health_notifier.messages[-1])
        self.assertEqual(len(d.health_notifier.messages), 2)


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

    def test_server_state_roundtrips(self):
        self.assertEqual(state.read_server(), (None, ""))
        state.set_server(False, "tunnel answered HTTP 502")
        self.assertEqual(state.read_server(), (False, "tunnel answered HTTP 502"))
        state.set_server(True)
        self.assertEqual(state.read_server(), (True, ""))

    def test_quit_all_stops_a_hand_started_daemon(self):
        import subprocess

        from vani import service

        proc = subprocess.Popen(["sleep", "60"])
        try:
            state.write_pidfile(paths.daemon_pidfile(), proc.pid)
            with mock.patch.object(service, "units_installed", return_value=False):
                done = service.quit_all()
            self.assertTrue(any("stopped daemon" in line for line in done))
            self.assertEqual(proc.wait(timeout=5), -signal.SIGTERM)
        finally:
            proc.kill()


if __name__ == "__main__":
    unittest.main()
