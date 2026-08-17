"""HTTP client for the transcription API.

Two endpoints matter: `GET /healthz` (no auth) and `POST /transcribe`, which
takes a raw WAV body and returns `{"text": ...}` or `{"error": ...}`.
Everything that can go wrong on the wire surfaces as TranscribeError with a
message short enough to put in a notification.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from . import __version__
from .config import Config

#: Sent on every request. Not cosmetic: the API sits behind Cloudflare, which
#: answers the default "Python-urllib/3.x" agent with 403 before the request
#: ever reaches the tunnel — every call fails, healthz included.
USER_AGENT = f"vani/{__version__}"


class TranscribeError(Exception):
    """The server could not be reached, or returned an error."""


class Client:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def transcribe(self, wav: bytes) -> str:
        """Send a WAV and return the transcript (possibly an empty string)."""
        req = urllib.request.Request(
            self.cfg.transcribe_url,
            data=wav,
            headers={
                "Authorization": "Bearer " + self.cfg.require_token(),
                "Content-Type": "audio/wav",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.server.timeout_sec) as r:
                payload = json.loads(r.read())
        except urllib.error.HTTPError as exc:
            raise TranscribeError(_http_message(exc)) from None
        except urllib.error.URLError as exc:
            raise TranscribeError(f"server unreachable ({exc.reason})") from None
        except (OSError, json.JSONDecodeError) as exc:
            raise TranscribeError(str(exc)) from None

        if not isinstance(payload, dict):
            raise TranscribeError("unexpected response from server")
        if "text" not in payload:
            raise TranscribeError(str(payload.get("error", "unknown server error")))
        return str(payload["text"]).strip()

    def health(self) -> dict[str, Any]:
        req = urllib.request.Request(
            self.cfg.health_url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as exc:
            raise TranscribeError(_http_message(exc)) from None
        except urllib.error.URLError as exc:
            raise TranscribeError(f"server unreachable ({exc.reason})") from None
        except (OSError, json.JSONDecodeError) as exc:
            raise TranscribeError(str(exc)) from None


def _http_message(exc: urllib.error.HTTPError) -> str:
    if exc.code == 401:
        return "401 unauthorized — check the API token"
    detail = ""
    try:
        body = json.loads(exc.read())
        detail = str(body.get("error") or body.get("detail") or "")
    except Exception:
        pass
    return f"HTTP {exc.code}" + (f": {detail[:120]}" if detail else "")
