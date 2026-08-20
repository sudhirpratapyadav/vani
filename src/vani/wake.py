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
    """Vosk against a grammar of just the wake phrases.

    Two things stop it firing at everything. First, a phrase must appear as a
    run of whole words — a substring test matched "hey claude" inside "hey
    Claudia". Second, and this is what does the real work, a match in a
    *partial* result only counts once it has survived `confirm_sec` of further
    audio. Partials are volatile: the decoder flickers through "hey claude"
    on its way to "hey [unk]" for a phrase that merely rhymes, then revises
    it away a chunk later. Waiting a quarter of a second to see whether the
    match sticks separated every true wake from every false one in testing,
    at the cost of about 0.25 s of latency. A match in a *final* result needs
    no such wait — the decoder has already settled.
    """

    def __init__(self, model_dir: Path, phrases: list[str], rate: int,
                 confirm_sec: float = 0.25):
        self.phrases = [p.strip().lower() for p in phrases if p.strip()]
        self.rate = rate
        self.confirm_sec = max(0.0, confirm_sec)
        self._words = [p.split() for p in self.phrases]
        self._held = -1.0
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
        self._held = -1.0

    def _heard(self, text: str) -> bool:
        """Is a wake phrase present as a run of whole words?"""
        said = text.split()
        for parts in self._words:
            n = len(parts)
            for i in range(len(said) - n + 1):
                if said[i:i + n] == parts:
                    return True
        return False

    def feed(self, chunk: bytes) -> bool:
        if self._rec.AcceptWaveform(chunk):
            self._held = -1.0
            # The decoder has committed to this; no confirmation needed.
            return self._heard(json.loads(self._rec.Result()).get("text", ""))
        partial = json.loads(self._rec.PartialResult()).get("partial", "")
        if not self._heard(partial):
            self._held = -1.0
            return False
        if self._held < 0:
            self._held = 0.0          # first sighting; start the clock
        else:
            self._held += len(chunk) / (self.rate * 2)
        if self._held < self.confirm_sec:
            return False
        self._held = -1.0
        return True

    @property
    def describe(self) -> str:
        return ", ".join('"%s"' % p for p in self.phrases)


def build(cfg, rate: int) -> Spotter:
    """Create the configured spotter, or NullSpotter when wake words are off."""
    if not cfg.wake.enabled:
        return NullSpotter()
    return VoskSpotter(cfg.model_path, cfg.wake.phrases, rate,
                       cfg.wake.confirm_sec)
