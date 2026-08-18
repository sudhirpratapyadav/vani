"""Health-check behaviour, with urlopen stubbed out."""
from __future__ import annotations

import io
import unittest
import urllib.error
from unittest import mock

from vani import client
from vani.client import ServerError, check_health
from vani.config import Config


def ok_response():
    stub = mock.MagicMock()
    stub.__enter__.return_value.read.return_value = b""
    return stub


def config() -> Config:
    cfg = Config()
    cfg.server.url = "wss://x.test/v1/realtime"
    return cfg


class HealthTest(unittest.TestCase):
    def test_healthy_server_passes(self):
        with mock.patch("urllib.request.urlopen", return_value=ok_response()):
            check_health(config())  # must not raise

    def test_probes_the_https_side_of_the_wss_host(self):
        with mock.patch("urllib.request.urlopen", return_value=ok_response()) as op:
            check_health(config())
        self.assertEqual(op.call_args[0][0].full_url, "https://x.test/health")

    def test_tunnel_502_is_reported_as_server_down(self):
        err = urllib.error.HTTPError("u", 502, "Bad Gateway", {}, io.BytesIO(b""))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaisesRegex(ServerError, "down"):
                check_health(config())

    def test_unreachable_host(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("refused")):
            with self.assertRaisesRegex(ServerError, "unreachable"):
                check_health(config())

    def test_every_request_names_itself(self):
        """Cloudflare 403s the default Python-urllib agent, health included."""
        with mock.patch("urllib.request.urlopen", return_value=ok_response()) as op:
            check_health(config())
        agent = op.call_args[0][0].get_header("User-agent")
        self.assertEqual(agent, client.USER_AGENT)
        self.assertNotIn("Python-urllib", agent)


if __name__ == "__main__":
    unittest.main()
