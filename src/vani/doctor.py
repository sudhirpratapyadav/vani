"""`vani doctor` — check everything dictation depends on, in one pass.

Most failures in an app like this are environmental (a missing binary, an
expired token, a Wayland session, the GPU server being down) rather than bugs,
so the first debugging step should be one command that names the problem.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass

from . import paths
from .client import ServerError, check_health
from .config import Config, ConfigError
from .config import load as load_config
from .output import detect_typer, session_type

OK, WARN, FAIL = "ok", "warn", "fail"
MARKS = {OK: "✓", WARN: "!", FAIL: "✗"}


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""


def run(check_server: bool = True) -> int:
    checks: list[Check] = []
    checks += _config_checks()
    cfg = None
    try:
        cfg = load_config()
    except ConfigError:
        pass

    checks += _binary_checks()
    checks += _mic_check(cfg)
    checks += _session_checks(cfg)
    checks += _wake_checks(cfg)
    checks += _stream_checks(cfg)
    checks += _service_checks()
    if check_server and cfg is not None:
        checks.append(_server_check(cfg))

    width = max(len(c.name) for c in checks)
    for check in checks:
        line = f"  {MARKS[check.status]} {check.name.ljust(width)}"
        print(f"{line}  {check.detail}" if check.detail else line)

    failed = [c for c in checks if c.status == FAIL]
    warned = [c for c in checks if c.status == WARN]
    print()
    if failed:
        print(f"{len(failed)} problem(s) to fix before dictation will work.")
        return 1
    print("All required checks passed."
          + (f" ({len(warned)} optional item(s) missing)" if warned else ""))
    return 0


def _config_checks() -> list[Check]:
    path = paths.config_file()
    if not path.exists():
        return [Check("config", FAIL, f"missing — run `vani config init` ({path})")]
    checks = [Check("config", OK, str(path))]

    mode = path.stat().st_mode & 0o077
    if mode:
        checks.append(Check("config permissions", WARN,
                            f"world/group readable — chmod 600 {path}"))
    try:
        cfg = load_config(path)
    except ConfigError as exc:
        return checks + [Check("config contents", FAIL, str(exc))]

    try:
        token = cfg.resolved_token()
        checks.append(Check("api token", OK, "" if token else "not set (optional)"))
    except ConfigError as exc:
        checks.append(Check("api token", FAIL, str(exc)))
    checks.append(Check("server url", OK, cfg.server.url))
    return checks


def _binary_checks() -> list[Check]:
    checks = []
    for name, why, required in (
        ("arecord", "recording (package: alsa-utils)", True),
        ("xinput", "media-key watching", False),
        ("gdbus", "desktop notifications", False),
        ("pactl", "naming the input device in logs", False),
    ):
        found = shutil.which(name)
        checks.append(Check(name, OK if found else (FAIL if required else WARN),
                            found or f"not found — needed for {why}"))
    return checks


def _mic_check(cfg: Config | None) -> list[Check]:
    from . import audio

    device = cfg.recording.device if cfg else ""
    if not device:
        return [Check("microphone", OK,
                      f"system default ({audio.default_source()}) — pin one "
                      "with `vani mic`")]
    if audio.is_alsa_name(device) or audio._source_present(device):
        return [Check("microphone", OK, device)]
    if device.startswith("bluez_source."):
        return [Check("microphone", WARN,
                      f"{device} is off (headset in A2DP) — vani switches the "
                      "profile when recording starts")]
    return [Check("microphone", FAIL, f"{device} not found — `vani mic list`")]


def _session_checks(cfg: Config | None) -> list[Check]:
    kind = session_type()
    checks = [Check("session", OK if kind == "x11" else WARN, kind)]
    if kind == "wayland":
        checks.append(Check("", WARN, "  the media-key watcher needs X11; "
                                      "typing needs ydotool + its daemon"))
    backend = detect_typer() if (cfg is None or cfg.output.typer == "auto") \
        else cfg.output.typer
    status = {"xdotool": OK, "ydotool": OK, "clipboard": WARN, "stdout": FAIL}[backend]
    detail = {
        "clipboard": "no typing tool — transcripts will be copied, not typed",
        "stdout": "no way to deliver text (install xdotool)",
    }.get(backend, "")
    checks.append(Check("typing backend", status, f"{backend} {detail}".strip()))
    return checks


def _wake_checks(cfg: Config | None) -> list[Check]:
    if cfg is not None and not cfg.wake.enabled:
        return [Check("wake word", OK, "disabled in config (hotkey only)")]
    checks = []
    try:
        import vosk  # noqa: F401

        checks.append(Check("vosk", OK))
    except ModuleNotFoundError:
        return [Check("vosk", WARN, "not installed — `pip install --user vosk`; "
                                    "hotkey dictation still works")]
    model = cfg.model_path if cfg else paths.model_dir()
    if model.is_dir():
        checks.append(Check("wake model", OK, str(model)))
    else:
        checks.append(Check("wake model", WARN,
                            f"missing — run `vani model download` ({model})"))
    return checks


def _stream_checks(cfg: Config | None) -> list[Check]:
    try:
        import websockets  # noqa: F401
    except ModuleNotFoundError:
        return [Check("websockets", FAIL,
                      "not installed and there is no other transcription path — "
                      "`pip install --user 'websockets>=12'`")]
    return [Check("websockets", OK)]


def _service_checks() -> list[Check]:
    from . import daemon, service

    pid = daemon.is_running()
    checks = [Check("daemon", OK if pid else WARN,
                    f"running (pid {pid})" if pid else "not running — `vani service start`")]
    if shutil.which("systemctl"):
        if not service.units_installed():
            checks.append(Check("services", WARN,
                                "units not installed — rerun ./install.sh"))
        else:
            active, enabled = service.unit_state(service.DAEMON_UNIT)
            checks.append(Check(
                "start on login", OK if enabled == "enabled" else WARN,
                enabled if enabled == "enabled"
                else "disabled — `vani service enable`"))
    return checks


def _server_check(cfg: Config) -> Check:
    try:
        check_health(cfg)
    except ServerError as exc:
        return Check("server", FAIL, f"{cfg.health_url}: {exc}")
    return Check("server", OK, cfg.server.url)


def environment_summary() -> str:
    """One-line context for bug reports."""
    return (f"session={session_type()} display={os.environ.get('DISPLAY', '-')} "
            f"runtime={paths.runtime_dir()}")
