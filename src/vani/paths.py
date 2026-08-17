"""Where vani keeps its files.

Everything follows the XDG basedir spec, so the whole app is removable with
`rm -rf ~/.config/vani ~/.cache/vani ~/.local/share/vani`.

  ~/.config/vani/config.toml            configuration (mode 600 — holds the token)
  ~/.local/share/vani/<vosk-model>/     wake-word model
  ~/.cache/vani/history.log             transcript history
  ~/.cache/vani/last.wav                last audio sent, for debugging
  $XDG_RUNTIME_DIR/vani/                volatile state shared between processes
"""
from __future__ import annotations

import os
from pathlib import Path

APP = "vani"

#: Wake-word model this app is built against; `vani doctor` checks for it.
VOSK_MODEL_NAME = "vosk-model-small-en-us-0.15"
VOSK_MODEL_URL = f"https://alphacephei.com/vosk/models/{VOSK_MODEL_NAME}.zip"


def _home(env: str, default: str) -> Path:
    return Path(os.environ.get(env) or Path.home() / default).expanduser()


def config_dir() -> Path:
    return _home("XDG_CONFIG_HOME", ".config") / APP


def config_file() -> Path:
    return config_dir() / "config.toml"


def data_dir() -> Path:
    return _home("XDG_DATA_HOME", ".local/share") / APP


def model_dir() -> Path:
    return data_dir() / VOSK_MODEL_NAME


def cache_dir() -> Path:
    return _home("XDG_CACHE_HOME", ".cache") / APP


def history_file() -> Path:
    return cache_dir() / "history.log"


def last_wav() -> Path:
    return cache_dir() / "last.wav"


def runtime_dir() -> Path:
    """Volatile per-session state; falls back to /tmp when XDG_RUNTIME_DIR is unset."""
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/{APP}-{os.getuid()}"
    return Path(base) / APP


def status_file() -> Path:
    return runtime_dir() / "status"


def daemon_pidfile() -> Path:
    return runtime_dir() / "daemon.pid"


def toggle_pidfile() -> Path:
    return runtime_dir() / "toggle-arecord.pid"


def toggle_wav() -> Path:
    return runtime_dir() / "toggle.wav"


def legacy_config_file() -> Path:
    """The pre-rename shell-style config, imported by `vani config init`."""
    return _home("XDG_CONFIG_HOME", ".config") / "dictate" / "config"


def ensure_dirs() -> None:
    """Create every directory vani writes to. Safe to call repeatedly."""
    for d in (config_dir(), data_dir(), cache_dir(), runtime_dir()):
        d.mkdir(parents=True, exist_ok=True)
