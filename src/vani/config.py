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


#: The batch API this app used before v2; recognised only to migrate old configs.
LEGACY_BATCH_URL = "https://ai.lsquarelabs.com"

#: Docking anchors the pill understands (see tray.py).
UI_POSITIONS = ("bottom-center", "bottom-left", "bottom-right",
                "top-center", "top-left", "top-right")

#: Live-caption display modes (see UiConfig.captions).
UI_CAPTIONS = ("always", "hover", "off")

#: Transcription back ends vani can speak to (see stream.py).
PROVIDERS = ("auto", "voxtral", "deepgram")

#: Deepgram's realtime endpoint, and the host that identifies it.
DEEPGRAM_HOST = "api.deepgram.com"
DEEPGRAM_URL = "wss://api.deepgram.com/v1/listen"


@dataclass
class ServerConfig:
    #: Which realtime protocol the endpoint speaks. "auto" reads it off the
    #: URL — api.deepgram.com means Deepgram, anything else is the
    #: OpenAI-realtime-shaped Voxtral server — so the URL alone is usually
    #: enough. Set it explicitly to override that guess.
    provider: str = "auto"
    #: Realtime ASR WebSocket endpoint.
    url: str = "wss://api.deepgram.com/v1/listen"
    #: Optional bearer token, sent as an Authorization header when set.
    #: Prefer `token_file` or the VANI_TOKEN env var on shared machines.
    token: str = ""
    #: Optional path to a file holding the token (first line).
    token_file: str = ""
    #: Model name: sent in session.update for Voxtral, as ?model= for Deepgram.
    model: str = "nova-3"
    #: After a recording ends, give up once the server has been silent this
    #: long. Counted from its last delta, not from the stop — a long
    #: utterance keeps draining as long as words keep arriving.
    timeout_sec: float = 20.0
    #: Minutes between background connectivity checks (0 disables them).
    health_check_min: float = 5.0


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
    #: arecord device to record from (`arecord -L` lists them). Empty = the
    #: system default source — which can silently change, e.g. when a
    #: Bluetooth headset flips between A2DP (no mic) and HFP profiles.
    device: str = ""
    #: Capture rate; the API expects 16 kHz mono.
    sample_rate: int = 16000
    #: Speech is this many times louder than the tracked ambient noise floor.
    speech_factor: float = 3.5
    #: Absolute level speech must clear in a silent room. Lower it for a quiet
    #: microphone; vani also scales it down automatically (see session.py).
    min_speech_level: float = 350.0


@dataclass
class HotkeyConfig:
    #: Watch a physical key via raw input events.
    enabled: bool = True
    #: 171 = XF86AudioNext, the F9 key without Fn on many laptops.
    #: Find yours with: xinput test-xi2 --root | grep -A2 RawKeyPress
    #: (an X keycode; the evdev backend subtracts 8 internally).
    keycode: int = 171
    #: Ignore a new press this soon after the previous one.
    debounce_sec: float = 0.5
    #: How the key is watched: auto picks evdev on Wayland (needs membership
    #: in the `input` group) and xinput on X11.
    backend: str = "auto"
    #: Holding the key at least this long makes the press push-to-talk:
    #: recording runs while held and is sent the moment the key is released.
    hold_sec: float = 0.35
    #: Holding the key this long *during* a hands-free recording cancels it.
    cancel_hold_sec: float = 0.6


@dataclass
class UiConfig:
    """The pill instrument and caption card, drawn by the tray process."""

    #: Show the pill at all. Without it (or without the tray process),
    #: session feedback falls back to notifications.
    enabled: bool = True
    #: Caption card width in pixels; text wraps inside it.
    width: int = 520
    #: The caption card grows with the text up to this height, then scrolls.
    max_height: int = 260
    #: Pill and card background opacity, 0.2 (glassy) to 1.0 (solid).
    opacity: float = 0.88
    #: Where the pill docks: bottom-center, bottom-left, bottom-right,
    #: top-center, top-left, top-right.
    position: str = "bottom-center"
    #: Live captions while recording: "always" shows the draft card the whole
    #: time, "hover" only while the pointer is over the pill (calm by default,
    #: evidence on demand), "off" never.
    captions: str = "always"
    #: While idle with the wake word armed, show a tiny dim dot (amber when
    #: the server is unreachable). Off = nothing on screen while idle.
    idle_dot: bool = True
    #: Earcons: rising tick on start, falling tick on typed, low buzz on
    #: trouble, soft pop on wake-word acknowledge. Played by the daemon.
    sounds: bool = True


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
    ui: UiConfig = field(default_factory=UiConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    #: Where this config was read from (empty when built from defaults).
    source: str = ""

    # -- derived values ----------------------------------------------------

    @property
    def provider(self) -> str:
        """Which realtime protocol this config actually speaks."""
        if self.server.provider != "auto":
            return self.server.provider
        host = self.server.url.partition("://")[2].split("/", 1)[0]
        return "deepgram" if host == DEEPGRAM_HOST else "voxtral"

    @property
    def health_url(self) -> str:
        """A plain-HTTP endpoint that answers "is this service up?".

        vLLM answers GET /health on the same host that serves the socket.
        Deepgram has no such endpoint, so the cheapest equivalent is a
        listing call — it also proves the key is accepted, which /health
        never did.
        """
        if self.provider == "deepgram":
            return "https://api.deepgram.com/v1/projects"
        scheme, _, rest = self.server.url.partition("://")
        host = rest.split("/", 1)[0]
        return ("https" if scheme == "wss" else "http") + f"://{host}/health"

    @property
    def model_path(self) -> Path:
        if self.wake.model_dir:
            return Path(self.wake.model_dir).expanduser()
        return paths.model_dir()

    def resolved_token(self) -> str:
        """The token from the env var, the token file, or the config, in that order.

        DEEPGRAM_API_KEY is honoured too when that is the provider — it is the
        name the vendor's own tooling uses, so a machine that already exports
        it needs nothing in the config file.
        """
        env = os.environ.get("VANI_TOKEN")
        if not env and self.provider == "deepgram":
            env = os.environ.get("DEEPGRAM_API_KEY")
        if env:
            return env.strip()
        if self.server.token_file:
            path = Path(self.server.token_file).expanduser()
            try:
                return path.read_text().strip()
            except OSError as exc:
                raise ConfigError(f"cannot read token_file {path}: {exc}") from None
        return self.server.token.strip()



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
    _migrate_legacy(data)
    for section_name in ("server", "wake", "recording", "hotkey", "ui", "output"):
        section = data.pop(section_name, {})
        if not isinstance(section, dict):
            raise ConfigError(f"{path}: [{section_name}] must be a table")
        _fill(getattr(cfg, section_name), section, section_name, path)
    if data:
        raise ConfigError(f"{path}: unknown section(s): {', '.join(sorted(data))}")
    _validate(cfg)
    return _apply_env(cfg)


def _migrate_legacy(data: dict) -> None:
    """Absorb a config written for the batch-era app without erroring.

    The keys that changed meaning are mapped, not rejected: an app update must
    not strand the user at "unknown key" over settings an older vani wrote
    itself. The token is the one thing worth carrying over verbatim.
    """
    recording = data.get("recording")
    if isinstance(recording, dict):
        # Post-hoc gain made sense for a batch upload; a live stream sends the
        # audio as captured, so the knob no longer exists.
        recording.pop("auto_gain", None)
        # v2 is streaming-only, so there is no transport to choose any more.
        # The old `vani config init` wrote this key itself, so rejecting it
        # would strand every pre-v2 user at "unknown key recording.transport".
        recording.pop("transport", None)
    server = data.get("server")
    if isinstance(server, dict):
        server.pop("endpoint", None)  # batch-only concept
        url = server.get("url", "")
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            # The old batch base URL cannot stream; point it at the default
            # realtime endpoint rather than failing on scheme validation.
            server.pop("url")
        if isinstance(server.get("timeout_sec"), (int, float)) \
                and server["timeout_sec"] > 60:
            server.pop("timeout_sec")  # batch-scale value; use the streaming default
    stream = data.pop("stream", None)
    if isinstance(stream, dict):  # a short-lived interim section; fold it in
        if not isinstance(server, dict):
            server = data["server"] = {}
        for old, new in (("url", "url"), ("model", "model"),
                         ("done_timeout_sec", "timeout_sec")):
            if old in stream:
                server.setdefault(new, stream[old])


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
    for name in ("silence_sec", "max_sec", "min_sec", "sample_rate",
                 "speech_factor", "min_speech_level"):
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
    if not cfg.server.url.startswith(("ws://", "wss://")):
        raise ConfigError("server.url must start with ws:// or wss://")
    if cfg.server.timeout_sec <= 0:
        raise ConfigError("server.timeout_sec must be positive")
    if cfg.server.health_check_min < 0:
        raise ConfigError("server.health_check_min must not be negative")
    if cfg.ui.width <= 0 or cfg.ui.max_height <= 0:
        raise ConfigError("ui.width and ui.max_height must be positive")
    if not 0.2 <= cfg.ui.opacity <= 1.0:
        raise ConfigError("ui.opacity must be between 0.2 and 1.0")
    if cfg.ui.position not in UI_POSITIONS:
        raise ConfigError("ui.position must be one of: " + ", ".join(UI_POSITIONS))
    if cfg.server.provider not in PROVIDERS:
        raise ConfigError("server.provider must be one of: " + ", ".join(PROVIDERS))
    if cfg.ui.captions not in UI_CAPTIONS:
        raise ConfigError("ui.captions must be one of: " + ", ".join(UI_CAPTIONS))
    if cfg.hotkey.backend not in ("auto", "xinput", "evdev"):
        raise ConfigError("hotkey.backend must be auto, xinput, or evdev")
    if cfg.hotkey.hold_sec <= 0 or cfg.hotkey.cancel_hold_sec <= 0:
        raise ConfigError("hotkey.hold_sec and cancel_hold_sec must be positive")


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
# This file may hold an API token; keep it mode 600.

[server]
provider = "{provider}"   # auto | deepgram | voxtral — auto reads it off the url
url = "{url}"
model = "{model}"
token = "{token}"
# token_file = "~/.config/vani/token"   # alternative to the line above
# Deepgram also accepts the key from $DEEPGRAM_API_KEY.
timeout_sec = {timeout_sec}     # wait for the final transcript after a recording

[wake]
enabled = {wake_enabled}
phrases = [{phrases}]
# Every word must exist in the Vosk model's vocabulary.

[recording]
silence_sec = {silence_sec}        # silence that ends a recording
silence_warn_sec = {warn_sec}   # silence before the countdown appears
max_sec = {max_sec}          # hard limit on one recording

[hotkey]
enabled = {hotkey_enabled}
keycode = {keycode}          # XF86AudioNext; find yours with:
                     # xinput test-xi2 --root | grep -A2 RawKeyPress

[output]
typer = "{typer}"       # auto | xdotool | ydotool | clipboard | stdout
notify = {notify}
history = {history}
"""


def render(cfg: Config) -> str:
    """The annotated starter file written by `vani config init`.

    This is a readable subset — every key it *does* write reflects `cfg`, but
    the rarely-touched ones are left out so a fresh config stays short. Use
    `dump` when the output has to account for every setting.
    """
    phrases = ", ".join('"%s"' % p.replace('"', '\\"') for p in cfg.wake.phrases)
    return TEMPLATE.format(
        provider=cfg.server.provider,
        model=cfg.server.model,
        url=cfg.server.url,
        token=cfg.server.token,
        timeout_sec=_num(cfg.server.timeout_sec),
        wake_enabled=_bool(cfg.wake.enabled),
        phrases=phrases,
        silence_sec=_num(cfg.recording.silence_sec),
        warn_sec=_num(cfg.recording.silence_warn_sec),
        max_sec=_num(cfg.recording.max_sec),
        hotkey_enabled=_bool(cfg.hotkey.enabled),
        keycode=cfg.hotkey.keycode,
        typer=cfg.output.typer,
        notify=_bool(cfg.output.notify),
        history=_bool(cfg.output.history),
    )


def dump(cfg: Config, *, mask_token: bool = False) -> str:
    """Every effective value as TOML, one line per field.

    `vani config show` has to be trustworthy when something is behaving oddly,
    so this is generated from the dataclasses rather than from a template: a
    setting cannot be silently absent from the output, and a field added later
    shows up here without anyone remembering to update a string.
    """
    out: list[str] = []
    for name in ("server", "wake", "recording", "hotkey", "ui", "output"):
        section = getattr(cfg, name)
        out.append(f"[{name}]")
        for spec in fields(section):
            value = getattr(section, spec.name)
            if mask_token and (name, spec.name) == ("server", "token") and value:
                value = "***"
            out.append(f"{spec.name} = {_toml(value)}")
        out.append("")
    if os.environ.get("VANI_TOKEN"):
        out.append("# note: $VANI_TOKEN is set and overrides server.token above")
    return "\n".join(out)


def _toml(value: Any) -> str:
    if isinstance(value, bool):  # before int: bool is a subclass of it
        return _bool(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _num(value)
    return '"%s"' % str(value).replace("\\", "\\\\").replace('"', '\\"')


def _bool(value: bool) -> str:
    return "true" if value else "false"


def _num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def set_key(section: str, key: str, value: Any, path: Path | None = None) -> Path:
    """Update one key in the config file, leaving everything else untouched.

    A settings UI must not round-trip through render(), which writes only the
    starter subset and would silently drop any other key the user customised.
    This edits the file as text: replace the key's line if present, insert it
    under its section header otherwise, append the section if it is missing.
    """
    path = path or paths.config_file()
    lines = path.read_text().splitlines() if path.exists() else []
    new_line = f"{key} = {_toml(value)}"
    out: list[str] = []
    in_section = False
    replaced = inserted = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            if in_section and not replaced and not inserted:
                out.append(new_line)   # section ended without the key
                inserted = True
            in_section = stripped == f"[{section}]"
        elif in_section and not replaced \
                and stripped.split("=")[0].strip() == key:
            line = new_line
            replaced = True
        out.append(line)
    if in_section and not replaced and not inserted:
        out.append(new_line)
    if not replaced and not inserted and not in_section \
            and f"[{section}]" not in (l.strip() for l in out):
        out += ["", f"[{section}]", new_line]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n")
    path.chmod(0o600)
    return path


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
    # DICTATE_URL is deliberately not imported: it named the old batch API,
    # which nothing in this app can talk to any more.
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
