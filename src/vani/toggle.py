"""Push-to-talk: one press starts recording, the next sends it.

Bind `vani toggle` to a normal keyboard shortcut in the desktop settings for
people who would rather not have a daemon listening, or as a fallback when the
media-key watcher can't see the key.

If the daemon is running it gets a SIGUSR1 instead — one process should own the
microphone, and this way the two mechanisms share the same recording.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time

from . import audio, daemon, paths, state
from .client import Client, TranscribeError
from .config import Config, ConfigError
from .notify import Notifier
from .output import Typist


def toggle(cfg: Config) -> int:
    if daemon.signal_toggle():
        return 0
    paths.ensure_dirs()
    pid = state.read_pidfile(paths.toggle_pidfile())
    return _stop_and_send(cfg) if pid else _start(cfg)


def _start(cfg: Config) -> int:
    notifier = Notifier(cfg.output.notify)
    wav = paths.toggle_wav()
    wav.unlink(missing_ok=True)
    try:
        proc = subprocess.Popen(
            ["arecord", "-q", "-f", "S16_LE", "-r", str(cfg.recording.sample_rate),
             "-c", "1", str(wav)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        notifier.show(f"Cannot record: {exc}", 5000)
        return 1
    state.write_pidfile(paths.toggle_pidfile(), proc.pid)
    state.set_status(state.RECORDING)
    notifier.show("● Recording — press the key again to send", 60000)
    return 0


def _stop_and_send(cfg: Config) -> int:
    notifier = Notifier(cfg.output.notify)
    pid = state.read_pidfile(paths.toggle_pidfile())
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    state.clear_pidfile(paths.toggle_pidfile())
    time.sleep(0.2)  # let arecord write the WAV header

    state.set_status(state.TRANSCRIBING)
    notifier.show("Transcribing...", 60000, replace=True)
    try:
        pcm = audio.read_wav(str(paths.toggle_wav()), cfg.recording.sample_rate)
    except (OSError, ValueError) as exc:
        state.set_status(state.IDLE)
        notifier.show(f"Nothing recorded: {exc}", 4000, replace=True)
        return 1

    if audio.duration(pcm, cfg.recording.sample_rate) < cfg.recording.min_sec:
        state.set_status(state.IDLE)
        notifier.show("(nothing captured, cancelled)", 2500, replace=True)
        return 0

    if cfg.recording.auto_gain:
        pcm, _ = audio.auto_gain(pcm)
    wav = audio.to_wav(pcm, cfg.recording.sample_rate)
    if cfg.output.save_last_wav:
        paths.last_wav().write_bytes(wav)

    try:
        text = Client(cfg).transcribe(wav)
    except (TranscribeError, ConfigError) as exc:
        state.set_status(state.IDLE)
        notifier.show(f"Failed: {exc}", 6000, replace=True)
        return 1

    if not text:
        state.set_status(state.IDLE)
        notifier.show("(no speech detected)", 3000, replace=True)
        return 0

    daemon.deliver(text, Typist(cfg.output.typer, cfg.output.type_delay_ms),
                   notifier, cfg)
    state.set_status(state.IDLE)
    return 0
