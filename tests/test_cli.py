"""CLI surface and the state/history files.

These use a temporary HOME so nothing touches the developer's real config.
"""
from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from vani import paths, state
from vani.cli import main


class TempHome(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp())
        self.env = mock.patch.dict(os.environ, {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_CACHE_HOME": str(self.home / ".cache"),
            "XDG_DATA_HOME": str(self.home / ".local/share"),
            "XDG_RUNTIME_DIR": str(self.home / "run"),
        })
        self.env.start()
        paths.ensure_dirs()

    def tearDown(self) -> None:
        self.env.stop()

    def run_cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(list(argv))
        return code, out.getvalue()


class CliTest(TempHome):
    def test_version(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli("--version")
        self.assertEqual(ctx.exception.code, 0)

    def test_no_command_prints_help(self):
        code, out = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("vani", out)

    def test_config_init_then_show(self):
        code, out = self.run_cli("config", "init")
        self.assertEqual(code, 0)
        self.assertTrue(paths.config_file().exists())
        self.assertEqual(paths.config_file().stat().st_mode & 0o777, 0o600)

        code, out = self.run_cli("config", "show")
        self.assertEqual(code, 0)
        self.assertIn("[server]", out)

    def test_config_init_refuses_to_clobber(self):
        self.run_cli("config", "init")
        paths.config_file().write_text('[server]\nurl = "https://kept.test"\n')
        code, _ = self.run_cli("config", "init")
        self.assertEqual(code, 1)
        self.assertIn("kept.test", paths.config_file().read_text())

    def test_config_show_masks_the_token(self):
        self.run_cli("config", "init")
        paths.config_file().write_text(
            '[server]\nurl = "https://x.test"\ntoken = "supersecret"\n')
        _, out = self.run_cli("config", "show")
        self.assertNotIn("supersecret", out)
        self.assertIn("***", out)

    def test_commands_needing_config_fail_cleanly(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli("toggle")
        self.assertEqual(ctx.exception.code, 2)

    def test_status_without_a_daemon(self):
        code, out = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("idle", out)
        self.assertIn("not running", out)

    def test_history_empty_then_populated(self):
        code, out = self.run_cli("history")
        self.assertIn("no transcripts", out)
        state.append_history("hello world")
        code, out = self.run_cli("history")
        self.assertIn("hello world", out)

    def test_history_respects_the_limit(self):
        for i in range(5):
            state.append_history(f"line {i}")
        _, out = self.run_cli("history", "-n", "2")
        self.assertIn("line 4", out)
        self.assertNotIn("line 0", out)


class StateTest(TempHome):
    def test_status_roundtrip(self):
        state.set_status(state.RECORDING)
        self.assertEqual(state.read_status(), (state.RECORDING, 0.0))

    def test_countdown_roundtrip(self):
        state.set_countdown(1.5)
        kind, seconds = state.read_status()
        self.assertEqual(kind, state.SILENCE)
        self.assertAlmostEqual(seconds, 1.5)

    def test_garbage_status_reads_as_idle(self):
        paths.status_file().write_text("???")
        self.assertEqual(state.read_status(), (state.IDLE, 0.0))

    def test_missing_status_reads_as_idle(self):
        self.assertEqual(state.read_status(), (state.IDLE, 0.0))

    def test_history_is_newest_first(self):
        state.append_history("first")
        state.append_history("second")
        entries = state.read_history()
        self.assertEqual([text for _, text in entries], ["second", "first"])

    def test_history_survives_tabs_in_text(self):
        state.append_history("a\tb")
        self.assertEqual(state.read_history()[0][1], "a b")

    def test_pidfile_of_a_dead_process(self):
        pidfile = paths.runtime_dir() / "test.pid"
        state.write_pidfile(pidfile, 999_999)
        self.assertIsNone(state.read_pidfile(pidfile))

    def test_pidfile_of_this_process(self):
        pidfile = paths.runtime_dir() / "test.pid"
        state.write_pidfile(pidfile)
        self.assertEqual(state.read_pidfile(pidfile), os.getpid())


if __name__ == "__main__":
    unittest.main()
