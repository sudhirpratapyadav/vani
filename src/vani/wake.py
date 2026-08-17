"""Wake-word spotting.

Vosk runs a small local model constrained to a grammar of just the wake
phrases plus `[unk]`, so it is looking for a handful of words rather than
transcribing the room — cheap enough to leave running all day, and it never
sends idle audio anywhere.

Vosk is an optional dependency: without it (or with `wake.enabled = false`)
the daemon still works as a hotkey-driven recorder.
"""
from __future__ import annotations

import json
from pathlib import Path


class WakeError(Exception):
    """The wake-word model could not be loaded."""


class Spotter:
    """Interface: feed audio chunks, get True when a wake phrase is heard."""

    def feed(self, chunk: bytes) -> bool:
        raise NotImplementedError

    def reset(self) -> None:
        pass

    @property
    def describe(self) -> str:
        return "disabled"


class NullSpotter(Spotter):
    """No wake word; the hotkey is the only way in."""

    def feed(self, chunk: bytes) -> bool:
        return False


class VoskSpotter(Spotter):
    def __init__(self, model_dir: Path, phrases: list[str], rate: int):
        self.phrases = [p.strip().lower() for p in phrases if p.strip()]
        self.rate = rate
        if not self.phrases:
            raise WakeError("no wake phrases configured")
        if not model_dir.is_dir():
            raise WakeError(
                f"no wake-word model at {model_dir} — run `vani model download`")
        try:
            from vosk import Model, SetLogLevel
        except ModuleNotFoundError:
            raise WakeError(
                "the vosk package is not installed — `pip install --user vosk` "
                "or set wake.enabled = false") from None
        SetLogLevel(-1)
        try:
            self._model = Model(str(model_dir))
        except Exception as exc:  # vosk raises bare Exception on a bad model dir
            raise WakeError(f"cannot load model at {model_dir}: {exc}") from None
        self._grammar = json.dumps(self.phrases + ["[unk]"])
        self.reset()

    def reset(self) -> None:
        from vosk import KaldiRecognizer

        self._rec = KaldiRecognizer(self._model, self.rate, self._grammar)

    def feed(self, chunk: bytes) -> bool:
        if self._rec.AcceptWaveform(chunk):
            text = json.loads(self._rec.Result()).get("text", "")
        else:
            text = json.loads(self._rec.PartialResult()).get("partial", "")
        return any(p in text for p in self.phrases)

    @property
    def describe(self) -> str:
        return ", ".join('"%s"' % p for p in self.phrases)


def build(cfg, rate: int) -> Spotter:
    """Create the configured spotter, or NullSpotter when wake words are off."""
    if not cfg.wake.enabled:
        return NullSpotter()
    return VoskSpotter(cfg.model_path, cfg.wake.phrases, rate)
