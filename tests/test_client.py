"""API client behaviour, with urlopen stubbed out."""
from __future__ import annotations

import io
import json
import unittest
import urllib.error
from unittest import mock

from vani.client import Client, TranscribeError
from vani.config import Config, ConfigError


def response(payload: dict):
    body = json.dumps(payload).encode()
    stub = mock.MagicMock()
    stub.__enter__.return_value.read.return_value = body
    return stub


def config() -> Config:
    cfg = Config()
    cfg.server.url = "https://x.test"
    cfg.server.token = "tok"
    return cfg


class ClientTest(unittest.TestCase):
    def test_returns_text(self):
        with mock.patch("urllib.request.urlopen", return_value=response({"text": " hi "})):
            self.assertEqual(Client(config()).transcribe(b"wav"), "hi")

    def test_sends_the_bearer_token(self):
        with mock.patch("urllib.request.urlopen", return_value=response({"text": "x"})) as op:
            Client(config()).transcribe(b"wav")
        request = op.call_args[0][0]
        self.assertEqual(request.get_header("Authorization"), "Bearer tok")
        self.assertEqual(request.full_url, "https://x.test/transcribe")

    def test_missing_token_raises_config_error(self):
        cfg = config()
        cfg.server.token = ""
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ConfigError):
                Client(cfg).transcribe(b"wav")

    def test_server_error_payload(self):
        with mock.patch("urllib.request.urlopen",
                        return_value=response({"error": "model not loaded"})):
            with self.assertRaises(TranscribeError) as ctx:
                Client(config()).transcribe(b"wav")
        self.assertIn("model not loaded", str(ctx.exception))

    def test_unauthorized_is_explained(self):
        err = urllib.error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(b"{}"))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(TranscribeError) as ctx:
                Client(config()).transcribe(b"wav")
        self.assertIn("token", str(ctx.exception))

    def test_unreachable_server(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("refused")):
            with self.assertRaises(TranscribeError) as ctx:
                Client(config()).transcribe(b"wav")
        self.assertIn("unreachable", str(ctx.exception))

    def test_empty_transcript_is_not_an_error(self):
        with mock.patch("urllib.request.urlopen", return_value=response({"text": ""})):
            self.assertEqual(Client(config()).transcribe(b"wav"), "")

    def test_health(self):
        with mock.patch("urllib.request.urlopen", return_value=response({"ready": True})):
            self.assertEqual(Client(config()).health(), {"ready": True})


if __name__ == "__main__":
    unittest.main()
