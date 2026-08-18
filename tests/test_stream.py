"""LiveStream against a scripted socket: no network, no server.

The fake socket answers `recv` from a queue of scripted events, so the
threaded pump/reader machinery runs for real while the wire stays imaginary.
"""
from __future__ import annotations

import json
import queue
import time
import unittest

from vani.config import Config
from vani.stream import LiveStream, StreamError


class FakeSocket:
    """Feeds scripted server events to recv(); records everything sent."""

    def __init__(self, script: list[dict]):
        self.sent: list[dict] = []
        self.closed = False
        self._events: "queue.Queue[dict]" = queue.Queue()
        for event in script:
            self._events.put(event)

    def send(self, data: str) -> None:
        if self.closed:
            raise OSError("socket closed")
        self.sent.append(json.loads(data))

    def recv(self, timeout: float | None = None) -> str:
        try:
            return json.dumps(self._events.get(timeout=min(timeout or 2, 2)))
        except queue.Empty:
            raise OSError("connection closed") from None

    def close(self) -> None:
        self.closed = True


def make_stream(script: list[dict], on_delta=lambda t: None):
    ws = FakeSocket(script)
    stream = LiveStream(Config(), on_delta, open_socket=lambda cfg: ws)
    return stream, ws


class LiveStreamTest(unittest.TestCase):
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

        stream = LiveStream(Config(), open_socket=refuse)
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


if __name__ == "__main__":
    unittest.main()
