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
            # A PipeWire/Pulse source name is pinned via the pulse ALSA plugin
            # and $PULSE_SOURCE; a raw ALSA name goes straight to -D.
            cmd += ["-D", self.device if is_alsa_name(self.device) else "pulse"]
        return cmd + ["-"]

    def _env(self) -> "dict[str, str] | None":
        if self.device and not is_alsa_name(self.device):
            import os

            device = self.device
            if device.startswith("bluez_source."):
                # Bind to the live node: the profile suffix in the persisted
                # name can drift across reconnects, the device prefix cannot.
                prefix = ".".join(device.split(".")[:2])
                device = next((n for n, _d in list_sources()
                               if n.startswith(prefix)), device)
            return {**os.environ, "PULSE_SOURCE": device}
        return None

    def open(self) -> None:
        self.close()
        if self.device and not is_alsa_name(self.device):
            # A Bluetooth mic only exists while its card is in the right
            # profile; re-assert it on every open, because the card falls
            # back to A2DP whenever the headset reconnects.
            ensure_source(self.device)
        self._proc = subprocess.Popen(
            self._command(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=self._env())

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


# --------------------------------------------------------------------------
# Source selection (PipeWire/Pulse via pactl)


def is_alsa_name(device: str) -> bool:
    """True for raw ALSA device strings, False for Pulse/PipeWire source names."""
    return device.startswith(("hw:", "plughw:", "default", "sysdefault",
                              "pulse", "pipewire", "dsnoop"))


def _pactl(*args: str) -> str:
    try:
        return subprocess.run(["pactl", *args], capture_output=True,
                              text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def list_sources() -> "list[tuple[str, str]]":
    """(name, description) of every real input, monitor loopbacks excluded."""
    out: "list[tuple[str, str]]" = []
    name = ""
    for line in _pactl("list", "sources").splitlines():
        line = line.strip()
        if line.startswith("Name: "):
            name = line[len("Name: "):]
        elif line.startswith("Description: ") and name:
            if not name.endswith(".monitor"):
                out.append((name, line[len("Description: "):]))
            name = ""
    return out


def bluetooth_mic_candidates() -> "list[tuple[str, str, str]]":
    """(card, profile, description) of Bluetooth mics that exist but are off.

    A headset in its A2DP profile has no source at all; the mic appears only
    once the card switches to a headset profile. These are shown alongside
    real sources so choosing one is possible before it exists.
    """
    cards: "list[tuple[str, str, str]]" = []
    card = desc = ""
    profiles: "list[str]" = []
    active = ""

    def flush() -> None:
        if card.startswith("bluez_card.") and profiles:
            best = profiles[0]
            if best != active:
                cards.append((card, best, desc or card))

    for line in _pactl("list", "cards").splitlines():
        stripped = line.strip()
        if stripped.startswith("Name: "):
            flush()
            card, desc, profiles, active = stripped[len("Name: "):], "", [], ""
        elif stripped.startswith("device.description = "):
            desc = stripped.split("=", 1)[1].strip().strip('"')
        elif stripped.startswith("Active Profile: "):
            active = stripped[len("Active Profile: "):]
        elif ": " in stripped and "sources: 1" in stripped \
                and "available: yes" in stripped:
            profiles.append(stripped.split(": ", 1)[0])
    flush()
    return cards


def _source_present(name: str) -> bool:
    """Exact match, except Bluetooth sources match on their device prefix:
    the node's profile suffix varies (and can differ from the profile name),
    but `bluez_source.<mac>` is stable across reconnects."""
    sources = [n for n, _d in list_sources()]
    if name in sources:
        return True
    if name.startswith("bluez_source."):
        prefix = ".".join(name.split(".")[:2])
        return any(n.startswith(prefix) for n in sources)
    return False


def ensure_source(name: str, wait_sec: float = 3.0) -> bool:
    """Make the named source exist if switching a Bluetooth profile can.

    Returns True when the source is present. Safe to call when it already is.
    """
    import time

    if _source_present(name):
        return True
    if not name.startswith("bluez_source."):
        return False
    mac = name.split(".")[1] if "." in name else ""
    for card, profile, _desc in bluetooth_mic_candidates():
        if card == f"bluez_card.{mac}":
            _pactl("set-card-profile", card, profile)
            break
    else:
        return False
    deadline = time.time() + wait_sec
    while time.time() < deadline:
        if _source_present(name):
            return True
        time.sleep(0.2)
    return False
