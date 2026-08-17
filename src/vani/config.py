"""Configuration: defaults, the TOML file, and environment overrides.

Precedence is environment > config file > defaults. Unknown keys are an error
rather than being ignored — a typo in `silence_sec` would otherwise leave the
old default in place and look like a bug in the daemon.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from . import paths
from .toml_lite import TomlError, loads


class ConfigError(Exception):
    """The configuration is missing, unparseable, or invalid."""


@dataclass
class ServerConfig:
    #: Base URL of the transcription API.
    url: str = "https://ai.lsquarelabs.com"
    #: Bearer token. Prefer `token_file` or the VANI_TOKEN env var on shared machines.
    token: str = ""
    #: Optional path to a file holding the token (first line).
    token_file: str = ""
    #: Endpoint appended to `url` for raw-WAV transcription.
    endpoint: str = "/transcribe"
    #: How long to wait for a transcription, in seconds.
    timeout_sec: float = 120.0


@dataclass
class WakeConfig:
    #: Wake-word spotting needs the Vosk model; turn off for hotkey-only use.
    enabled: bool = True
    #: Phrases that start a recording. Every word must exist in the model vocabulary.
    phrases: list[str] = field(default_factory=lambda: ["hey claude", "hi claude"])
    #: Directory of the Vosk model; empty means the default under ~/.local/share/vani.
    model_dir: str = ""


@dataclass
class RecordingConfig:
    #: Silence that ends a recording and sends it.
    silence_sec: float = 3.0
    #: Silence before the countdown starts showing.
    silence_warn_sec: float = 1.0
    #: Hard cap on a single recording.
    max_sec: float = 120.0
    #: Recordings shorter than this are discarded as accidental.
    min_sec: float = 0.4
    #: Trailing silence left on the clip so words aren't clipped.
    keep_tail_sec: float = 0.4
    #: Capture rate; the API expects 16 kHz mono.
    sample_rate: int = 16000
    #: Amplify quiet input before sending (helps Bluetooth headsets in HFP mode).
    auto_gain: bool = True


@dataclass
class HotkeyConfig:
    #: Watch a physical key via X raw input events.
    enabled: bool = True
    #: 171 = XF86AudioNext, the F9 key without Fn on many laptops.
    #: Find yours with: xinput test-xi2 --root | grep -A2 RawKeyPress
    keycode: int = 171
    #: Ignore repeats within this window.
    debounce_sec: float = 0.5


@dataclass
class OutputConfig:
    #: How to deliver text: auto, xdotool, ydotool, clipboard, or stdout.
    typer: str = "auto"
    #: Per-keystroke delay in milliseconds when typing.
    type_delay_ms: int = 2
    #: Show desktop notifications.
    notify: bool = True
    #: Append transcripts to the history log.
    history: bool = True
    #: Keep the last clip at ~/.cache/vani/last.wav for debugging.
    save_last_wav: bool = True


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    wake: WakeConfig = field(default_factory=WakeConfig)
    recording: RecordingConfig = field(default_factory=RecordingConfig)
    hotkey: HotkeyConfig = field(default_factory=HotkeyConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    #: Where this config was read from (empty when built from defaults).
    source: str = ""

    # -- derived values ----------------------------------------------------

    @property
    def transcribe_url(self) -> str:
        return self.server.url.rstrip("/") + "/" + self.server.endpoint.lstrip("/")

    @property
    def health_url(self) -> str:
        return self.server.url.rstrip("/") + "/healthz"

    @property
    def model_path(self) -> Path:
        if self.wake.model_dir:
            return Path(self.wake.model_dir).expanduser()
        return paths.model_dir()

    def resolved_token(self) -> str:
        """The token from the env var, the token file, or the config, in that order."""
        env = os.environ.get("VANI_TOKEN")
        if env:
            return env.strip()
        if self.server.token_file:
            path = Path(self.server.token_file).expanduser()
            try:
                return path.read_text().strip()
            except OSError as exc:
                raise ConfigError(f"cannot read token_file {path}: {exc}") from None
        return self.server.token.strip()

    def require_token(self) -> str:
        token = self.resolved_token()
        if not token:
            raise ConfigError(
                "no API token — set server.token in "
                f"{paths.config_file()}, point server.token_file at a file, "
                "or export VANI_TOKEN"
            )
        return token


# --------------------------------------------------------------------------
# Loading


def load(path: Path | None = None, *, required: bool = True) -> Config:
    """Read the config file, or return defaults when it is absent and optional."""
    path = path or paths.config_file()
    if not path.exists():
        if required:
            raise ConfigError(
                f"no config at {path} — run `vani config init` to create one"
            )
        return _apply_env(Config())
    try:
        data = loads(path.read_text())
    except TomlError as exc:
        raise ConfigError(f"{path}: {exc}") from None
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from None

    cfg = Config(source=str(path))
    for section_name in ("server", "wake", "recording", "hotkey", "output"):
        section = data.pop(section_name, {})
        if not isinstance(section, dict):
            raise ConfigError(f"{path}: [{section_name}] must be a table")
        _fill(getattr(cfg, section_name), section, section_name, path)
    if data:
        raise ConfigError(f"{path}: unknown section(s): {', '.join(sorted(data))}")
    _validate(cfg)
    return _apply_env(cfg)


def _fill(obj: Any, values: dict[str, Any], section: str, path: Path) -> None:
    known = {f.name: f for f in fields(obj)}
    for key, value in values.items():
        spec = known.get(key)
        if spec is None:
            hint = _closest(key, known)
            raise ConfigError(
                f"{path}: unknown key {section}.{key}"
                + (f" (did you mean {section}.{hint}?)" if hint else "")
            )
        setattr(obj, key, _coerce(value, getattr(obj, key), f"{section}.{key}"))


def _coerce(value: Any, current: Any, label: str) -> Any:
    if isinstance(current, bool):
        if not isinstance(value, bool):
            raise ConfigError(f"{label} must be true or false")
        return value
    if isinstance(current, list):
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f"{label} must be a list of strings")
        return value
    if isinstance(current, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{label} must be a number")
        return float(value)
    if isinstance(current, int):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{label} must be an integer")
        return value
    if not isinstance(value, str):
        raise ConfigError(f"{label} must be a string")
    return value


def _closest(key: str, known: dict[str, Any]) -> str | None:
    import difflib

    match = difflib.get_close_matches(key, list(known), n=1, cutoff=0.7)
    return match[0] if match else None


def _validate(cfg: Config) -> None:
    r = cfg.recording
    if r.silence_warn_sec > r.silence_sec:
        raise ConfigError("recording.silence_warn_sec must not exceed silence_sec")
    for name in ("silence_sec", "max_sec", "min_sec", "sample_rate"):
        if getattr(r, name) <= 0:
            raise ConfigError(f"recording.{name} must be positive")
    if r.max_sec <= r.min_sec:
        raise ConfigError("recording.max_sec must be greater than min_sec")
    if cfg.output.typer not in ("auto", "xdotool", "ydotool", "clipboard", "stdout"):
        raise ConfigError(
            "output.typer must be one of: auto, xdotool, ydotool, clipboard, stdout"
        )
    if cfg.wake.enabled and not [p for p in cfg.wake.phrases if p.strip()]:
        raise ConfigError("wake.enabled is true but wake.phrases is empty")
    if not cfg.server.url.startswith(("http://", "https://")):
        raise ConfigError("server.url must start with http:// or https://")


def _apply_env(cfg: Config) -> Config:
    """VANI_URL / VANI_TOKEN override the file, for one-off runs and testing."""
    url = os.environ.get("VANI_URL")
    if url:
        cfg.server.url = url
    return cfg


# --------------------------------------------------------------------------
# Writing


TEMPLATE = """\
# vani configuration — see `vani config show` for the effective values.
# This file holds an API token; keep it mode 600.

[server]
url = "{url}"
token = "{token}"
# token_file = "~/.config/vani/token"   # alternative to the line above
timeout_sec = 120

[wake]
enabled = {wake_enabled}
phrases = [{phrases}]
# Every word must exist in the Vosk model's vocabulary.

[recording]
silence_sec = {silence_sec}        # silence that ends a recording
silence_warn_sec = {warn_sec}   # silence before the countdown appears
max_sec = {max_sec}          # hard limit on one recording
auto_gain = true

[hotkey]
enabled = true
keycode = {keycode}          # XF86AudioNext; find yours with:
                     # xinput test-xi2 --root | grep -A2 RawKeyPress

[output]
typer = "auto"       # auto | xdotool | ydotool | clipboard | stdout
notify = true
history = true
"""


def render(cfg: Config) -> str:
    phrases = ", ".join('"%s"' % p.replace('"', '\\"') for p in cfg.wake.phrases)
    return TEMPLATE.format(
        url=cfg.server.url,
        token=cfg.server.token,
        wake_enabled="true" if cfg.wake.enabled else "false",
        phrases=phrases,
        silence_sec=_num(cfg.recording.silence_sec),
        warn_sec=_num(cfg.recording.silence_warn_sec),
        max_sec=_num(cfg.recording.max_sec),
        keycode=cfg.hotkey.keycode,
    )


def _num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def write(cfg: Config, path: Path | None = None) -> Path:
    """Write the config with 600 permissions (it contains the token)."""
    path = path or paths.config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(cfg))
    path.chmod(0o600)
    return path


def from_legacy(path: Path | None = None) -> Config | None:
    """Build a Config from the old shell-style `~/.config/dictate/config`.

    Returns None when that file does not exist, so callers can fall back to
    plain defaults on a fresh machine.
    """
    import re

    path = path or paths.legacy_config_file()
    try:
        text = path.read_text()
    except OSError:
        return None

    values: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r'^\s*(?:export\s+)?([A-Z_]+)\s*=\s*"?([^"\n]*)"?\s*$', line)
        if m:
            values[m.group(1)] = m.group(2)

    cfg = Config(source=f"imported from {path}")
    if "DICTATE_URL" in values:
        cfg.server.url = values["DICTATE_URL"]
    if "DICTATE_TOKEN" in values:
        cfg.server.token = values["DICTATE_TOKEN"]
    if values.get("DICTATE_WAKE"):
        cfg.wake.phrases = [p.strip() for p in values["DICTATE_WAKE"].split("|") if p.strip()]
    for key, (obj, attr) in {
        "DICTATE_SILENCE_SEC": (cfg.recording, "silence_sec"),
        "DICTATE_SILENCE_WARN": (cfg.recording, "silence_warn_sec"),
        "DICTATE_MAXSEC": (cfg.recording, "max_sec"),
    }.items():
        if values.get(key):
            try:
                setattr(obj, attr, float(values[key]))
            except ValueError:
                pass
    return cfg
