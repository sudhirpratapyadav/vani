"""LiveStream against a scripted socket: no network, no server.

The fake socket answers `recv` from a queue of scripted events, so the
threaded pump/reader machinery runs for real while the wire stays imaginary.
Both dialects are covered: Deepgram (the default) and Voxtral.
"""
from __future__ import annotations

import json
import queue
import time
import unittest

from vani.config import Config
from vani import stream as stream_mod
from vani.stream import LiveStream, StreamError


class FakeSocket:
    """Feeds scripted server events to recv(); records everything sent.

    Text frames are decoded as JSON into `sent`; raw audio frames (Deepgram
    sends PCM with no envelope) are collected separately in `audio`.
    """

    def __init__(self, script: list[dict]):
        self.sent: list[dict] = []
        self.audio: list[bytes] = []
        self.closed = False
        self._events: "queue.Queue[dict]" = queue.Queue()
        for event in script:
            self._events.put(event)

    def send(self, data) -> None:
        if self.closed:
            raise OSError("socket closed")
        if isinstance(data, (bytes, bytearray)):
            self.audio.append(bytes(data))
        else:
            self.sent.append(json.loads(data))

    def recv(self, timeout: float | None = None) -> str:
        try:
            return json.dumps(self._events.get(timeout=min(timeout or 2, 2)))
        except queue.Empty:
            raise OSError("connection closed") from None

    def close(self) -> None:
        self.closed = True


def voxtral_config() -> Config:
    cfg = Config()
    cfg.server.provider = "voxtral"
    cfg.server.url = "wss://voxtral.test/v1/realtime"
    return cfg


def deepgram_config() -> Config:
    cfg = Config()
    cfg.server.provider = "deepgram"
    cfg.server.token = "test-key"
    return cfg


def make_stream(script: list[dict], on_delta=lambda t: None, cfg=None):
    ws = FakeSocket(script)
    stream = LiveStream(cfg or voxtral_config(), on_delta,
                        open_socket=lambda cfg: ws)
    return stream, ws


def make_deepgram(script: list[dict], on_delta=lambda t: None):
    return make_stream(script, on_delta, cfg=deepgram_config())


def results(text: str, final: bool) -> dict:
    return {"type": "Results", "is_final": final,
            "channel": {"alternatives": [{"transcript": text}]}}


class VoxtralStreamTest(unittest.TestCase):
    def test_deltas_accumulate_and_finish_returns_the_final_text(self):
        heard: list[str] = []
        stream, ws = make_stream([
            {"type": "transcription.delta", "delta": "hello"},
            {"type": "transcription.delta", "delta": " world"},
            {"type": "transcription.done", "text": "hello world"},
        ], heard.append)
        stream.start()
        stream.send(b"\x00\x00" * 1600)
        self.assertEqual(stream.finish(timeout=5), "hello world")
        # The callback saw the text growing, not isolated words.
        self.assertEqual(heard, ["hello", "hello world"])

    def test_the_wire_protocol_is_update_commit_append_final(self):
        stream, ws = make_stream([{"type": "transcription.done", "text": "ok"}])
        stream.start()
        stream.send(b"\x01\x02")
        stream.finish(timeout=5)
        kinds = [m["type"] for m in ws.sent]
        self.assertEqual(kinds, ["session.update", "input_audio_buffer.commit",
                                 "input_audio_buffer.append",
                                 "input_audio_buffer.commit"])
        self.assertNotIn("final", ws.sent[1])   # the opening commit starts the task
        self.assertTrue(ws.sent[-1]["final"])   # the closing one ends the utterance

    def test_done_without_text_falls_back_to_the_joined_deltas(self):
        stream, ws = make_stream([
            {"type": "transcription.delta", "delta": " pieced "},
            {"type": "transcription.delta", "delta": "together"},
            {"type": "transcription.done"},
        ])
        stream.start()
        self.assertEqual(stream.finish(timeout=5), "pieced together")

    def test_a_server_error_surfaces_in_finish(self):
        stream, ws = make_stream([{"type": "error", "error": "model exploded"}])
        stream.start()
        with self.assertRaisesRegex(StreamError, "model exploded"):
            stream.finish(timeout=5)

    def test_a_failed_connection_surfaces_in_finish_not_in_send(self):
        def refuse(cfg):
            raise OSError("connection refused")

        stream = LiveStream(voxtral_config(), open_socket=refuse)
        stream.start()
        stream.send(b"\x00\x00")  # must not raise: the mic loop calls this
        with self.assertRaisesRegex(StreamError, "connection refused"):
            stream.finish(timeout=5)

    def test_no_transcript_at_all_times_out(self):
        stream, ws = make_stream([])  # the server never answers
        stream.start()
        with self.assertRaisesRegex(StreamError, "no transcript"):
            stream.finish(timeout=0.2)

    def test_deltas_without_a_final_event_are_kept_not_discarded(self):
        """A long utterance can outlive any fixed wait; the words already in
        hand must be typed, not traded for an error notification."""
        stream, ws = make_stream([
            {"type": "transcription.delta", "delta": "nearly "},
            {"type": "transcription.delta", "delta": "everything"},
            # ...and transcription.done never arrives
        ])
        stream.start()
        self.assertEqual(stream.finish(timeout=0.5), "nearly everything")

    def test_server_activity_extends_the_wait(self):
        """The timeout bounds inactivity: a server still producing deltas is
        allowed to take longer in total than the timeout itself."""
        import threading

        stream, ws = make_stream([])
        stream.start()

        def drip() -> None:
            for i in range(4):
                time.sleep(0.3)   # each gap under the 0.5s timeout...
                ws._events.put({"type": "transcription.delta", "delta": str(i)})
            time.sleep(0.3)
            ws._events.put({"type": "transcription.done", "text": "0123 done"})

        threading.Thread(target=drip, daemon=True).start()
        started = time.time()
        self.assertEqual(stream.finish(timeout=0.5), "0123 done")
        self.assertGreater(time.time() - started, 1.0)  # ...totalling more

    def test_abort_closes_the_socket_and_poisons_finish(self):
        stream, ws = make_stream([])
        stream.start()
        stream.send(b"\x00\x00")
        stream.abort()
        stream._pump.join(timeout=2)  # the pump may still own the socket briefly
        self.assertTrue(ws.closed)
        with self.assertRaises(StreamError):
            stream.finish(timeout=1)


class DeepgramStreamTest(unittest.TestCase):
    """The dialect vani speaks by default."""

    def test_finals_accumulate_and_metadata_ends_the_stream(self):
        heard: list[str] = []
        stream, ws = make_deepgram([
            results("the quick brown", False),
            results("The quick brown fox.", True),
            results("Vani types", False),
            results("Vani types what you say.", True),
            {"type": "Metadata", "duration": 4.5},
        ], heard.append)
        stream.start()
        stream.send(b"\x00\x00" * 1600)
        self.assertEqual(stream.finish(timeout=5),
                         "The quick brown fox. Vani types what you say.")
        # Interims revise in place; they never append to themselves.
        self.assertEqual(heard, [
            "the quick brown",
            "The quick brown fox.",
            "The quick brown fox. Vani types",
            "The quick brown fox. Vani types what you say.",
        ])

    def test_audio_goes_up_raw_and_closestream_ends_it(self):
        stream, ws = make_deepgram([{"type": "Metadata"}])
        stream.start()
        stream.send(b"\x01\x02\x03")
        stream.finish(timeout=5)
        self.assertEqual(ws.audio, [b"\x01\x02\x03"])   # no base64, no envelope
        self.assertEqual([m["type"] for m in ws.sent], ["CloseStream"])

    def test_an_empty_final_settles_nothing(self):
        """Silent windows arrive as finals with no words; they must not
        punch blank gaps into the transcript."""
        stream, ws = make_deepgram([
            results("", True),
            results("Only this.", True),
            results("", True),
            {"type": "Metadata"},
        ])
        stream.start()
        self.assertEqual(stream.finish(timeout=5), "Only this.")

    def test_a_dropped_stream_keeps_the_finals_it_had(self):
        stream, ws = make_deepgram([
            results("Half a sentence", True),
            # ...and Metadata never arrives
        ])
        stream.start()
        self.assertEqual(stream.finish(timeout=0.5), "Half a sentence")

    def test_a_server_error_surfaces_in_finish(self):
        stream, ws = make_deepgram([
            {"type": "Error", "description": "invalid sample rate"},
        ])
        stream.start()
        with self.assertRaisesRegex(StreamError, "invalid sample rate"):
            stream.finish(timeout=5)


class DeepgramWireTest(unittest.TestCase):
    """URL and headers are built from the config, not hand-written."""

    def test_the_query_string_carries_the_audio_format(self):
        cfg = deepgram_config()
        cfg.recording.sample_rate = 16000
        cfg.server.model = "nova-3"
        url = stream_mod.socket_url(cfg)
        self.assertTrue(url.startswith("wss://api.deepgram.com/v1/listen?"))
        for expected in ("model=nova-3", "encoding=linear16",
                         "sample_rate=16000", "interim_results=true"):
            self.assertIn(expected, url)

    def test_a_hand_written_query_string_is_left_alone(self):
        cfg = deepgram_config()
        cfg.server.url = "wss://api.deepgram.com/v1/listen?model=nova-2&foo=bar"
        self.assertEqual(stream_mod.socket_url(cfg), cfg.server.url)

    def test_deepgram_uses_token_auth_voxtral_uses_bearer(self):
        self.assertEqual(stream_mod.socket_headers(deepgram_config())
                         ["Authorization"], "Token test-key")
        cfg = voxtral_config()
        cfg.server.token = "test-key"
        self.assertEqual(stream_mod.socket_headers(cfg)["Authorization"],
                         "Bearer test-key")

    def test_a_missing_key_is_refused_before_connecting(self):
        cfg = deepgram_config()
        cfg.server.token = ""
        with self.assertRaisesRegex(StreamError, "Deepgram API key"):
            stream_mod.socket_headers(cfg)

    def test_the_provider_is_read_off_the_url_when_unset(self):
        cfg = Config()
        cfg.server.provider = "auto"
        cfg.server.url = "wss://api.deepgram.com/v1/listen"
        self.assertEqual(cfg.provider, "deepgram")
        cfg.server.url = "wss://elsewhere.test/v1/realtime"
        self.assertEqual(cfg.provider, "voxtral")


if __name__ == "__main__":
    unittest.main()
