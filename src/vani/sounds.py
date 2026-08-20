"""Earcons: the audio half of the interface.

Dictation is an eyes-busy activity — the user is looking at their document,
not at the pill — so every state transition that matters has a sound: a soft
pop when the wake word is recognised ("speak after the pop"), a rising tick
when the microphone goes hot, a falling tick when the text has been typed,
and a low buzz for trouble (error, refusal, cancel). Four sounds, all short,
all quiet, all off with `ui.sounds = false`.

The WAVs are synthesised here with the stdlib and cached under
~/.cache/vani/sounds/ — no binary assets in the package, no audio
dependencies. Playback is fire-and-forget through paplay (PipeWire/Pulse) or
aplay; a machine with neither simply stays silent.
"""
from __future__ import annotations

import math
import shutil
import subprocess
import wave
from pathlib import Path

from . import paths

RATE = 22050
AMPLITUDE = 0.32  # of full scale — present, not startling
#: Bump when the synthesis changes so cached files regenerate.
GENERATION = 1

#: name -> list of (freq_hz, seconds) segments, played back to back.
#: A rising contour means "go", falling means "done" — the OS-wide convention.
CUES = {
    "wake": [(523.0, 0.07)],                       # soft pop: heard you
    "start": [(660.0, 0.06), (880.0, 0.07)],       # rising: mic is hot
    "done": [(880.0, 0.06), (660.0, 0.07)],        # falling: text delivered
    "trouble": [(196.0, 0.09), (165.0, 0.12)],     # low buzz: look at the pill
}


def _tone(freq: float, seconds: float) -> "list[int]":
    n = int(RATE * seconds)
    attack = int(RATE * 0.005)
    out = []
    for i in range(n):
        env = min(1.0, i / max(attack, 1)) * math.exp(-3.0 * i / n)
        out.append(int(AMPLITUDE * env * 32767
                       * math.sin(2 * math.pi * freq * i / RATE)))
    return out


def _write(path: Path, segments: "list[tuple[float, float]]") -> None:
    samples: "list[int]" = []
    for freq, seconds in segments:
        samples += _tone(freq, seconds)
    samples += [0] * int(RATE * 0.01)  # tail so the last cycle isn't clicked off
    import array

    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(array.array("h", samples).tobytes())


def ensure_files() -> Path:
    """Generate the cue files if missing or from an older generation."""
    directory = paths.sounds_dir()
    stamp = directory / f".gen-{GENERATION}"
    if not stamp.exists():
        directory.mkdir(parents=True, exist_ok=True)
        for old in directory.glob(".gen-*"):
            old.unlink()
        for name, segments in CUES.items():
            _write(directory / f"{name}.wav", segments)
        stamp.touch()
    return directory


class Player:
    """Fire-and-forget playback of the cue set; a no-op when disabled."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._cmd = None
        if enabled:
            if shutil.which("paplay"):
                self._cmd = ["paplay"]
            elif shutil.which("aplay"):
                self._cmd = ["aplay", "-q"]
            try:
                self._dir = ensure_files()
            except OSError:
                self._cmd = None

    def play(self, name: str) -> None:
        if not self.enabled or self._cmd is None or name not in CUES:
            return
        try:
            subprocess.Popen(self._cmd + [str(self._dir / f"{name}.wav")],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except OSError:
            self._cmd = None  # player vanished; stay silent from here on
