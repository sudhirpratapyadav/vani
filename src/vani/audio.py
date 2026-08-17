"""PCM helpers and the microphone source.

Audio is 16-bit signed little-endian mono throughout. The level maths is plain
Python over `array` rather than `audioop`: audioop was removed in Python 3.13,
and at 8 chunks a second the cost is irrelevant.

Capture goes through an `arecord` pipe, which keeps PortAudio and its build
dependencies out of the picture and lets the OS mixer pick the default source.
"""
from __future__ import annotations

import io
import math
import subprocess
import wave
from array import array
from typing import Iterator

SAMPLE_WIDTH = 2  # bytes per sample (S16_LE)


def samples(pcm: bytes) -> array:
    a = array("h")
    a.frombytes(pcm[: len(pcm) - len(pcm) % SAMPLE_WIDTH])
    return a


def peak(pcm: bytes) -> int:
    """Largest absolute sample value, 0..32768."""
    a = samples(pcm)
    if not a:
        return 0
    return max(max(a), -min(a))


def rms(pcm: bytes) -> float:
    """Root-mean-square level of a chunk, in raw sample units."""
    a = samples(pcm)
    if not a:
        return 0.0
    return math.sqrt(sum(s * s for s in a) / len(a))


def amplify(pcm: bytes, factor: float) -> bytes:
    """Scale samples by `factor`, clipping at the 16-bit range."""
    a = samples(pcm)
    for i, s in enumerate(a):
        v = int(s * factor)
        a[i] = 32767 if v > 32767 else (-32768 if v < -32768 else v)
    return a.tobytes()


def auto_gain(pcm: bytes, *, target: int = 24000, floor: int = 200,
              ceiling: int = 8000, max_factor: float = 8.0) -> tuple[bytes, float]:
    """Boost quiet-but-not-silent audio; returns (pcm, factor_applied).

    Bluetooth headsets in HFP mode record very quietly and the model then
    transcribes them as empty. Audio below `floor` is treated as silence and
    left alone so we don't amplify room noise into gibberish.
    """
    p = peak(pcm)
    if not (floor < p < ceiling):
        return pcm, 1.0
    factor = min(target / p, max_factor)
    return amplify(pcm, factor), factor


def to_wav(pcm: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(SAMPLE_WIDTH)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def read_wav(path: str, expect_rate: int) -> bytes:
    """Read a mono WAV at the expected rate, for --test-wav runs."""
    with wave.open(path, "rb") as w:
        if w.getnchannels() != 1 or w.getframerate() != expect_rate:
            raise ValueError(
                f"{path}: need {expect_rate} Hz mono, got "
                f"{w.getframerate()} Hz {w.getnchannels()}ch"
            )
        return w.readframes(w.getnframes())


def duration(pcm: bytes, rate: int) -> float:
    return len(pcm) / (rate * SAMPLE_WIDTH)


class Microphone:
    """An `arecord` pipe that can be restarted.

    Restarting matters: after a clip is sent, the pipe holds however much audio
    accumulated during the HTTP round-trip. Dropping and reopening it discards
    that stale input instead of feeding it to the next recording.
    """

    def __init__(self, rate: int, chunk_bytes: int, device: str | None = None):
        self.rate = rate
        self.chunk_bytes = chunk_bytes
        self.device = device
        self._proc: subprocess.Popen | None = None

    def _command(self) -> list[str]:
        cmd = ["arecord", "-q", "-f", "S16_LE", "-r", str(self.rate),
               "-c", "1", "-t", "raw"]
        if self.device:
            cmd += ["-D", self.device]
        return cmd + ["-"]

    def open(self) -> None:
        self.close()
        self._proc = subprocess.Popen(
            self._command(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def close(self) -> None:
        if self._proc is None:
            return
        proc, self._proc = self._proc, None
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            proc.kill()
            proc.wait()

    def chunks(self) -> Iterator[bytes]:
        """Yield fixed-size chunks until the pipe closes."""
        if self._proc is None or self._proc.stdout is None:
            raise RuntimeError("microphone is not open")
        while True:
            chunk = self._proc.stdout.read(self.chunk_bytes)
            if not chunk:
                return
            yield chunk

    def __enter__(self) -> "Microphone":
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def default_source() -> str:
    """Name of the PulseAudio/PipeWire default source, for log lines."""
    import re
    try:
        out = subprocess.run(["pactl", "info"], capture_output=True,
                             text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return "?"
    m = re.search(r"Default Source: (.+)", out)
    return m.group(1).strip() if m else "?"
