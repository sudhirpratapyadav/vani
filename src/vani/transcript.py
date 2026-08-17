"""The rolling transcript: what was said, when, and not for long.

One JSON object per line, newest appended, older than `stream.retain_days`
deleted. JSONL rather than a database because the file is the interface — the
agent reads it, `vani listen --tail` follows it, and a bad day is debugged with
`tail -f`. Audio is never written at all; frames go to the socket and are gone.

Expiry is enforced on write rather than by a timer, so a daemon that has been
down for a month still cleans up the moment it comes back.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from . import paths

#: How often to rewrite the file to drop expired lines, in seconds. Rewriting
#: on every append would be O(n) per utterance for no benefit.
SWEEP_INTERVAL_SEC = 3600.0


@dataclass
class Entry:
    """One finished utterance."""

    text: str
    at: float                      # unix seconds
    duration: float = 0.0
    #: True when the local gate scored this stretch as silence — the model
    #: talking to itself. Kept rather than dropped so the rate is measurable.
    suspect: bool = False

    @property
    def stamp(self) -> str:
        return time.strftime("%F %T", time.localtime(self.at))

    def to_json(self) -> str:
        d = {"at": round(self.at, 3), "text": self.text}
        if self.duration:
            d["duration"] = round(self.duration, 2)
        if self.suspect:
            d["suspect"] = True
        return json.dumps(d, ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> "Entry | None":
        try:
            d = json.loads(line)
            return cls(text=str(d["text"]), at=float(d["at"]),
                       duration=float(d.get("duration", 0.0)),
                       suspect=bool(d.get("suspect", False)))
        except (ValueError, KeyError, TypeError):
            return None  # a torn line from a killed process is not fatal


class Transcript:
    def __init__(self, path: Path | None = None, retain_days: float = 14.0):
        self.path = path or paths.transcript_file()
        self.retain_days = retain_days
        self._last_sweep = 0.0

    @property
    def _cutoff(self) -> float:
        return time.time() - self.retain_days * 86400

    def append(self, entry: Entry) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as f:
                f.write(entry.to_json() + "\n")
        except OSError:
            return  # never let the transcript take the daemon down
        if time.time() - self._last_sweep > SWEEP_INTERVAL_SEC:
            self.sweep()

    def read(self, limit: int | None = None, since: float = 0.0) -> list[Entry]:
        """Entries oldest first, expired ones excluded."""
        cutoff = max(self._cutoff, since)
        out = [e for e in self._iter() if e.at >= cutoff]
        return out[-limit:] if limit else out

    def sweep(self) -> int:
        """Drop expired entries. Returns how many were removed."""
        self._last_sweep = time.time()
        try:
            kept = [e for e in self._iter() if e.at >= self._cutoff]
        except OSError:
            return 0
        removed = sum(1 for _ in self._iter()) - len(kept)
        if removed <= 0:
            return 0
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text("".join(e.to_json() + "\n" for e in kept))
            os.replace(tmp, self.path)  # atomic: a reader never sees a partial file
        except OSError:
            return 0
        return removed

    def _iter(self) -> Iterator[Entry]:
        try:
            with self.path.open() as f:
                for line in f:
                    entry = Entry.from_json(line)
                    if entry is not None:
                        yield entry
        except OSError:
            return
