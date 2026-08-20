"""Connectivity to the ASR server.

Transcription itself rides the WebSocket in stream.py; what remains here is
knowing whether the server is there at all. vLLM answers `GET /health` with
an empty 200 on the same port that serves the realtime socket, so one cheap
HTTP request distinguishes "worth opening a stream" from "tell the user".
Everything that can go wrong on the wire surfaces as ServerError with a
message short enough to put in a notification.
"""
from __future__ import annotations

import urllib.error
import urllib.request

from . import __version__
from .config import Config

#: Sent on every request. Not cosmetic: the API sits behind Cloudflare, which
#: answers the default "Python-urllib/3.x" agent with 403 before the request
#: ever reaches the tunnel — every call fails, health included.
USER_AGENT = f"vani/{__version__}"


class ServerError(Exception):
    """The server could not be reached, or answered that it is not healthy."""


def check_health(cfg: Config, timeout: float = 10.0) -> None:
    """Raise ServerError unless the ASR service is reachable and usable."""
    headers = {"User-Agent": USER_AGENT}
    if cfg.provider == "deepgram":
        # Deepgram has no anonymous health endpoint; the listing call doubles
        # as a key check, so a wrong key is caught here rather than 20 s into
        # the first recording.
        token = cfg.resolved_token()
        if not token:
            raise ServerError("no Deepgram API key (set server.token or "
                              "$DEEPGRAM_API_KEY)")
        headers["Authorization"] = "Token " + token
    req = urllib.request.Request(cfg.health_url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return  # any 2xx means healthy; the body is empty
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ServerError("API key rejected (HTTP %d)" % exc.code) from None
        if exc.code in (502, 503, 504):
            raise ServerError("server is down (tunnel answered "
                              f"HTTP {exc.code})") from None
        raise ServerError(f"HTTP {exc.code} from {cfg.health_url}") from None
    except urllib.error.URLError as exc:
        raise ServerError(f"server unreachable ({exc.reason})") from None
    except OSError as exc:
        raise ServerError(str(exc)) from None
