"""Config parsing, validation, and the legacy import."""
from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path

from vani import config, toml_lite
from vani.config import Config, ConfigError

SAMPLE = """
# a comment
[server]
url = "https://example.test"
token = "secret"   # trailing comment

[wake]
enabled = true
phrases = ["hey there", "hi there"]

[recording]
silence_sec = 2.5
max_sec = 60
auto_gain = false
"""


class TomlLiteTest(unittest.TestCase):
    """Exercises the fallback parser directly, whatever the interpreter offers."""

    def parse(self, text: str) -> dict:
        return toml_lite._loads_fallback(text)

    def test_tables_and_scalars(self):
        data = self.parse(SAMPLE)
        self.assertEqual(data["server"]["url"], "https://example.test")
        self.assertEqual(data["server"]["token"], "secret")
        self.assertIs(data["wake"]["enabled"], True)
        self.assertEqual(data["recording"]["silence_sec"], 2.5)
        self.assertEqual(data["recording"]["max_sec"], 60)
        self.assertIs(data["recording"]["auto_gain"], False)

    def test_arrays_inline_and_multiline(self):
        self.assertEqual(self.parse('a = ["x", "y"]')["a"], ["x", "y"])
        multi = self.parse('a = [\n  "x",\n  "y",\n]')
        self.assertEqual(multi["a"], ["x", "y"])

    def test_hash_inside_a_string_is_not_a_comment(self):
        self.assertEqual(self.parse('a = "b#c"')["a"], "b#c")

    def test_dotted_table_names(self):
        self.assertEqual(self.parse("[a.b]\nc = 1")["a"]["b"]["c"], 1)

    def test_escapes(self):
        self.assertEqual(self.parse(r'a = "x\ny"')["a"], "x\ny")

    def test_bad_input_raises(self):
        for bad in ('a = ', 'a = [1, 2', 'a', 'a = {b = 1}'):
            with self.assertRaises(toml_lite.TomlError):
                self.parse(bad)

    def test_matches_stdlib_when_available(self):
        try:
            import tomllib
        except ModuleNotFoundError:
            self.skipTest("no tomllib on this interpreter")
        self.assertEqual(self.parse(SAMPLE), tomllib.loads(SAMPLE))


class ConfigLoadTest(unittest.TestCase):
    def write(self, text: str) -> Path:
        tmp = Path(tempfile.mkdtemp()) / "config.toml"
        tmp.write_text(text)
        return tmp

    def test_load_applies_values_and_keeps_defaults(self):
        cfg = config.load(self.write(SAMPLE))
        self.assertEqual(cfg.server.url, "https://example.test")
        self.assertEqual(cfg.wake.phrases, ["hey there", "hi there"])
        self.assertEqual(cfg.recording.silence_sec, 2.5)
        self.assertEqual(cfg.recording.silence_warn_sec, 1.0)  # untouched default
        self.assertEqual(cfg.hotkey.keycode, 171)

    def test_int_becomes_float_where_expected(self):
        cfg = config.load(self.write("[recording]\nsilence_sec = 4"))
        self.assertIsInstance(cfg.recording.silence_sec, float)

    def test_unknown_key_is_an_error_with_a_suggestion(self):
        with self.assertRaises(ConfigError) as ctx:
            config.load(self.write("[recording]\nsilence_secs = 4"))
        self.assertIn("silence_sec", str(ctx.exception))

    def test_unknown_section_is_an_error(self):
        with self.assertRaises(ConfigError):
            config.load(self.write("[nonsense]\nx = 1"))

    def test_wrong_type_is_an_error(self):
        with self.assertRaises(ConfigError):
            config.load(self.write('[hotkey]\nenabled = "yes"'))

    def test_validation_rejects_warn_longer_than_stop(self):
        with self.assertRaises(ConfigError):
            config.load(self.write("[recording]\nsilence_sec = 1\nsilence_warn_sec = 2"))

    def test_validation_rejects_unknown_typer(self):
        with self.assertRaises(ConfigError):
            config.load(self.write('[output]\ntyper = "telepathy"'))

    def test_missing_file(self):
        missing = Path(tempfile.mkdtemp()) / "nope.toml"
        with self.assertRaises(ConfigError):
            config.load(missing)
        self.assertEqual(config.load(missing, required=False).server.url,
                         Config().server.url)

    def test_urls(self):
        cfg = config.load(self.write('[server]\nurl = "https://x.test/"'))
        self.assertEqual(cfg.transcribe_url, "https://x.test/transcribe")
        self.assertEqual(cfg.health_url, "https://x.test/healthz")

    def test_token_from_file(self):
        token = Path(tempfile.mkdtemp()) / "token"
        token.write_text("abc123\n")
        cfg = config.load(self.write(f'[server]\ntoken_file = "{token}"'))
        self.assertEqual(cfg.require_token(), "abc123")

    def test_missing_token_raises(self):
        cfg = config.load(self.write('[server]\nurl = "https://x.test"'))
        with self.assertRaises(ConfigError):
            cfg.require_token()

    def test_render_roundtrips(self):
        original = config.load(self.write(SAMPLE))
        rewritten = config.load(self.write(config.render(original)))
        self.assertEqual(rewritten.server.url, original.server.url)
        self.assertEqual(rewritten.wake.phrases, original.wake.phrases)
        self.assertEqual(rewritten.recording.silence_sec, original.recording.silence_sec)
        self.assertIs(rewritten.recording.auto_gain, original.recording.auto_gain)


class DumpTest(unittest.TestCase):
    """`vani config show` has to report what the daemon will really use."""

    def write(self, text: str) -> Path:
        tmp = Path(tempfile.mkdtemp()) / "config.toml"
        tmp.write_text(text)
        return tmp

    def test_dump_shows_customised_values_not_defaults(self):
        cfg = config.load(self.write(
            '[server]\ntimeout_sec = 45\n[output]\ntyper = "stdout"\n'
            "type_delay_ms = 40\n[recording]\nmin_sec = 1.5\n"))
        text = config.dump(cfg)
        self.assertIn("timeout_sec = 45", text)
        self.assertIn('typer = "stdout"', text)
        self.assertIn("type_delay_ms = 40", text)
        self.assertIn("min_sec = 1.5", text)

    def test_dump_covers_every_field(self):
        cfg = Config()
        text = config.dump(cfg)
        for section in ("server", "wake", "recording", "hotkey", "output"):
            self.assertIn(f"[{section}]", text)
            for spec in dataclasses.fields(getattr(cfg, section)):
                self.assertIn(f"{spec.name} = ", text)

    def test_dump_roundtrips_through_the_loader(self):
        original = config.load(self.write(SAMPLE))
        rewritten = config.load(self.write(config.dump(original)))
        self.assertEqual(dataclasses.astuple(rewritten.recording),
                         dataclasses.astuple(original.recording))
        self.assertEqual(rewritten.server.token, original.server.token)
        self.assertEqual(rewritten.wake.phrases, original.wake.phrases)

    def test_dump_masks_the_token_on_request(self):
        cfg = config.load(self.write('[server]\ntoken = "supersecret"'))
        self.assertIn("supersecret", config.dump(cfg))
        self.assertNotIn("supersecret", config.dump(cfg, mask_token=True))


class LegacyImportTest(unittest.TestCase):
    def test_imports_the_old_shell_config(self):
        legacy = Path(tempfile.mkdtemp()) / "config"
        legacy.write_text(
            'DICTATE_URL="https://old.test"\n'
            'DICTATE_TOKEN="tok"\n'
            'DICTATE_WAKE="hey claude|hi claude"\n'
            'DICTATE_SILENCE_SEC="2"\n'
            'DICTATE_MAXSEC="90"\n')
        cfg = config.from_legacy(legacy)
        self.assertEqual(cfg.server.url, "https://old.test")
        self.assertEqual(cfg.server.token, "tok")
        self.assertEqual(cfg.wake.phrases, ["hey claude", "hi claude"])
        self.assertEqual(cfg.recording.silence_sec, 2.0)
        self.assertEqual(cfg.recording.max_sec, 90.0)

    def test_absent_legacy_config_returns_none(self):
        self.assertIsNone(config.from_legacy(Path("/nonexistent/dictate/config")))


if __name__ == "__main__":
    unittest.main()
