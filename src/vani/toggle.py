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
from .config import Config
from .notify import Notifier
from .output import Typist
from .stream import LiveStream, StreamError


def toggle(cfg: Config) -> int:
    if daemon.signal_toggle():
        return 0
    paths.ensure_dirs()
    return _stop_and_send(cfg) if _pending_clip() else _start(cfg)


def _pending_clip() -> bool:
    """Is a recording running, or a finished one waiting to be sent?

    arecord now exits by itself at `recording.max_sec`, which leaves a stale
    pidfile beside a perfectly good clip. The next press should send that clip
    rather than throw it away and start over.
    """
    pidfile = paths.toggle_pidfile()
    if not pidfile.exists():
        return False
    return state.read_pidfile(pidfile) is not None or paths.toggle_wav().exists()


def _start(cfg: Config) -> int:
    notifier = Notifier(cfg.output.notify)
    wav = paths.toggle_wav()
    wav.unlink(missing_ok=True)
    # -d stops at max_sec, so a forgotten push-to-talk cannot record all day.
    # Never 0, which arecord reads as "no limit".
    limit = max(1, round(cfg.recording.max_sec))
    try:
        proc = subprocess.Popen(
            ["arecord", "-q", "-f", "S16_LE", "-r", str(cfg.recording.sample_rate),
             "-c", "1", "-d", str(limit), str(wav)],
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

    if cfg.output.save_last_wav:
        state.save_last_wav(audio.to_wav(pcm, cfg.recording.sample_rate))

    # The same socket the daemon streams over, fed after the fact: the clip is
    # already complete here, so the deltas are not shown, only the final text.
    stream = LiveStream(cfg)
    stream.start()
    chunk_bytes = int(0.2 * cfg.recording.sample_rate * audio.SAMPLE_WIDTH)
    for i in range(0, len(pcm), chunk_bytes):
        stream.send(pcm[i:i + chunk_bytes])
    try:
        text = stream.finish(cfg.server.timeout_sec
                             + audio.duration(pcm, cfg.recording.sample_rate))
    except StreamError as exc:
        state.set_status(state.IDLE)
        notifier.show(f"✗ Transcription failed: {exc}", 8000, replace=True)
        return 1

    if not text:
        state.set_status(state.IDLE)
        notifier.show("(no speech detected)", 3000, replace=True)
        return 0

    daemon.deliver(text, Typist(cfg.output.typer, cfg.output.type_delay_ms),
                   notifier, cfg)
    state.set_status(state.IDLE)
    return 0
