"""The realtime ASR socket.

`mistralai/Voxtral-Mini-4B-Realtime-2602` on vLLM, behind an OpenAI-realtime
shaped WebSocket. The exchange is:

    -> session.update            {"model": ...}
    -> input_audio_buffer.commit                 starts the generation task
    -> input_audio_buffer.append {"audio": <base64 PCM16>}   ... repeatedly
    -> input_audio_buffer.commit {"final": true}             ends the utterance
    <- transcription.delta       {"delta": " word"}
    <- transcription.done        {"text": "the whole thing"}

Measured desktop-to-GPU through the tunnel: connect 0.6 s, first delta 0.7 s,
words arriving 0.3-0.5 s behind the speech.

`websockets` is an optional dependency, like vosk: without it v1 dictation is
unaffected and `vani listen` explains itself rather than dumping a traceback.
"""
from __future__ import annotations

import asyncio
import base64
import json
from typing import Awaitable, Callable

from .config import Config


class StreamError(Exception):
    """The realtime endpoint could not be reached, or refused the session."""


def _connect(cfg: Config):
    try:
        import websockets
    except ModuleNotFoundError:
        raise StreamError(
            "the websockets package is not installed — "
            "`pip install --user websockets`, or `pipx inject vani websockets`"
        ) from None
    # A default Python user agent is 403'd by Cloudflare in front of the tunnel;
    # see client.py, which learned the same lesson the hard way.
    return websockets.connect(
        cfg.stream.url, max_size=None,
        additional_headers={"User-Agent": "vani/2"},
        open_timeout=20, ping_interval=20, ping_timeout=20,
    )


class Utterance:
    """One open transcription on the socket, from activation to release."""

    def __init__(self, ws, model: str):
        self._ws = ws
        self._model = model
        self.text = ""

    async def start(self) -> None:
        await self._ws.send(json.dumps({"type": "session.update", "model": self._model}))
        # A commit *without* final is what starts the generation task.
        await self._ws.send(json.dumps({"type": "input_audio_buffer.commit"}))

    async def send(self, pcm: bytes) -> None:
        await self._ws.send(json.dumps({
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(pcm).decode(),
        }))

    async def finish(self) -> None:
        await self._ws.send(json.dumps({
            "type": "input_audio_buffer.commit", "final": True}))


async def transcribe(
    cfg: Config,
    audio_in: "asyncio.Queue[bytes | None]",
    on_delta: Callable[[str], None] = lambda t: None,
    on_done: Callable[[str], Awaitable[None] | None] = lambda t: None,
) -> None:
    """Stream one activation: pull audio off the queue until None, then finish.

    The queue carries whatever the gate returned, so the caller never has to
    know about pre-roll; `None` means the gate released and the utterance
    should be committed.
    """
    async with _connect(cfg) as ws:
        utt = Utterance(ws, cfg.stream.model)
        await utt.start()
        parts: list[str] = []

        async def pump() -> None:
            while True:
                chunk = await audio_in.get()
                if chunk is None:
                    break
                await utt.send(chunk)
            await utt.finish()

        pump_task = asyncio.create_task(pump())
        try:
            while True:
                try:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
                except asyncio.TimeoutError:
                    raise StreamError("no events from the server for 60s") from None
                kind = msg.get("type")
                if kind == "transcription.delta":
                    delta = msg.get("delta", "")
                    if delta:
                        parts.append(delta)
                        on_delta(delta)
                elif kind == "transcription.done":
                    text = (msg.get("text") or "".join(parts)).strip()
                    result = on_done(text)
                    if asyncio.iscoroutine(result):
                        await result
                    return
                elif kind == "error":
                    raise StreamError(str(msg.get("error", msg))[:200])
        finally:
            pump_task.cancel()
