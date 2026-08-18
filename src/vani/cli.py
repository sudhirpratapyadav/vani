"""Command-line interface: `vani <command>`.

Every command that needs configuration loads it here and turns ConfigError
into a one-line message, so no subcommand has to deal with a missing or broken
config file.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, config, model, paths, state
from .config import Config, ConfigError

PROG = "vani"
DESCRIPTION = "Voice dictation for the Linux desktop: speak, and the text is typed."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG, description=DESCRIPTION,
        epilog="Run `vani doctor` if dictation isn't working.")
    parser.add_argument("--version", action="version",
                        version=f"{PROG} {__version__}")
    parser.add_argument("-c", "--config", type=Path, metavar="PATH",
                        help=f"config file (default: {paths.config_file()})")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p = sub.add_parser("start", help="run the dictation daemon (foreground)")
    p.add_argument("--test-wav", metavar="FILE",
                   help="replay a 16 kHz mono WAV through the state machine and exit")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("toggle", help="start/stop a push-to-talk recording")
    p.set_defaults(func=cmd_toggle)

    p = sub.add_parser("tray", help="run the tray indicator")
    p.set_defaults(func=cmd_tray)

    p = sub.add_parser("status", help="show what the daemon is doing right now")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("service", help="manage the background services")
    p.add_argument("action",
                   choices=("status", "start", "stop", "restart",
                            "enable", "disable"),
                   help="enable/disable also control starting at login")
    p.set_defaults(func=cmd_service)

    p = sub.add_parser("quit", help="stop vani completely (daemon and tray)")
    p.set_defaults(func=cmd_quit)

    p = sub.add_parser("history", help="print past transcripts")
    p.add_argument("-n", "--lines", type=int, default=20,
                   help="how many to show (default 20, 0 for all)")
    p.add_argument("--paths", action="store_true", help="print the log path and exit")
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("doctor", help="check dependencies, config, and the server")
    p.add_argument("--offline", action="store_true", help="skip the server check")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("config", help="create or inspect the config file")
    p.add_argument("action", choices=("init", "path", "show", "edit"))
    p.add_argument("--force", action="store_true", help="overwrite an existing config")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("model", help="manage the wake-word model")
    p.add_argument("action", choices=("download", "path"))
    p.add_argument("--force", action="store_true", help="re-download if present")
    p.set_defaults(func=cmd_model)

    p = sub.add_parser("mic", help="list, select, or test the microphone")
    p.add_argument("action", nargs="?", default="list",
                   choices=("list", "set", "test"))
    p.add_argument("name", nargs="?",
                   help="for set: a number from `vani mic list`, a source "
                        "name, or 'default'")
    p.set_defaults(func=cmd_mic)

    p = sub.add_parser("say", help="transcribe a WAV file and print the text")
    p.add_argument("file", help="16 kHz mono WAV")
    p.add_argument("--type", action="store_true",
                   help="type the result into the focused window as well")
    p.set_defaults(func=cmd_say)

    return parser


# --------------------------------------------------------------------------
# Commands


def cmd_start(args: argparse.Namespace) -> int:
    from . import daemon as daemon_module

    cfg = _load(args, required=not args.test_wav)
    if args.test_wav:
        return daemon_module.run_test_wav(cfg, args.test_wav)

    running = daemon_module.is_running()
    if running:
        print(f"a vani daemon is already running (pid {running})", file=sys.stderr)
        return 1
    try:
        daemon_module.Daemon(cfg).run()
    except KeyboardInterrupt:
        return 0
    return 0


def cmd_toggle(args: argparse.Namespace) -> int:
    from .toggle import toggle

    return toggle(_load(args))


def cmd_tray(args: argparse.Namespace) -> int:
    from .tray import run

    return run()


def cmd_status(args: argparse.Namespace) -> int:
    from . import daemon as daemon_module

    current, countdown = state.read_status()
    pid = daemon_module.is_running()
    label = {
        state.IDLE: "idle",
        state.RECORDING: "recording",
        state.TRANSCRIBING: "transcribing",
        state.SILENCE: f"sending in {countdown:.1f}s",
    }[current]
    print(f"state:  {label}")
    print(f"daemon: {'running (pid %d)' % pid if pid else 'not running'}")
    ok, detail = state.read_server()
    if ok is None:
        print("server: unknown (no health check has run)")
    else:
        print(f"server: {'online' if ok else 'DOWN'}"
              + (f" — {detail}" if detail else ""))
    entries = state.read_history(1)
    if entries:
        stamp, text = entries[0]
        print(f"last:   {text[:80]}" + (f"  ({stamp})" if stamp else ""))
    return 0


def cmd_service(args: argparse.Namespace) -> int:
    from . import service

    if args.action == "status":
        for unit in service.UNITS:
            active, enabled = service.unit_state(unit)
            print(f"{unit:24} {active:10} start on login: {enabled}")
        return 0
    if args.action in ("enable", "disable"):
        ok, out = service.set_start_on_login(args.action == "enable")
        print(f"start on login: {'enabled' if args.action == 'enable' else 'disabled'}"
              if ok else f"error: {out or 'systemctl failed'}")
        return 0 if ok else 1
    ok, out = service.control(args.action)
    if not ok:
        print(f"error: {out or 'systemctl failed'}", file=sys.stderr)
        return 1
    past = {"start": "started", "stop": "stopped", "restart": "restarted"}
    print(f"{past[args.action]}: {', '.join(service.UNITS)}")
    return 0


def cmd_quit(args: argparse.Namespace) -> int:
    from . import service

    for line in service.quit_all():
        print(line)
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    if args.paths:
        print(paths.history_file())
        return 0
    entries = state.read_history(args.lines or None)
    if not entries:
        print("(no transcripts yet)")
        return 0
    for stamp, text in reversed(entries):
        print(f"{stamp}  {text}" if stamp else text)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from . import doctor

    print(f"{PROG} {__version__} — {doctor.environment_summary()}\n")
    return doctor.run(check_server=not args.offline)


def cmd_config(args: argparse.Namespace) -> int:
    path = args.config or paths.config_file()
    if args.action == "path":
        print(path)
        return 0
    if args.action == "init":
        return _config_init(path, args.force)
    if args.action == "edit":
        import os
        import subprocess

        editor = os.environ.get("EDITOR", "nano")
        if not path.exists():
            _config_init(path, force=False)
        return subprocess.call([editor, str(path)])

    cfg = _load(args)
    print(f"# effective configuration (from {cfg.source or 'defaults'})")
    # dump(), not render(): this must show what the daemon will actually use,
    # including the keys the starter template leaves out.
    print(config.dump(cfg, mask_token=True))
    return 0


def _config_init(path: Path, force: bool) -> int:
    if path.exists() and not force:
        print(f"{path} already exists (use --force to overwrite)", file=sys.stderr)
        return 1
    imported = config.from_legacy()
    cfg = imported or Config()
    config.write(cfg, path)
    if imported:
        print(f"imported settings from {paths.legacy_config_file()}")
    print(f"wrote {path}")
    print("\nNext: run `vani doctor` to check the setup, including the server.")
    return 0


def cmd_model(args: argparse.Namespace) -> int:
    if args.action == "path":
        print(paths.model_dir())
        return 0
    try:
        model.download(force=args.force)
    except model.ModelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def mic_choices() -> "list[tuple[str, str, tuple[str, str] | None]]":
    """(persist_name, label, bt_switch) — bt_switch names a (card, profile)
    that must be activated before the source exists. Shared with the tray."""
    from . import audio

    choices = [(name, desc, None) for name, desc in audio.list_sources()]
    for card, profile, desc in audio.bluetooth_mic_candidates():
        mac = card[len("bluez_card."):]
        choices.append((f"bluez_source.{mac}", f"{desc} (headset mic, off)",
                        (card, profile)))
    return choices


def cmd_mic(args: argparse.Namespace) -> int:
    from . import audio, service

    cfg = _load(args)
    current = cfg.recording.device

    if args.action == "list":
        print(f"  current: {current or 'system default (%s)' % audio.default_source()}\n")
        for i, (name, label, bt) in enumerate(mic_choices(), 1):
            mark = "*" if name == current or (bt and current.startswith(name)) else " "
            print(f"{mark} {i}. {label}\n       {name}")
        print(f"\n  0. system default\n\nselect with: vani mic set <number>")
        return 0

    if args.action == "test":
        return _mic_test(cfg)

    # set
    if not args.name:
        print("usage: vani mic set <number|source-name|default>", file=sys.stderr)
        return 2
    choices = mic_choices()
    target, bt = args.name, None
    if target in ("0", "default"):
        target = ""
    elif target.isdigit():
        idx = int(target) - 1
        if not 0 <= idx < len(choices):
            print(f"no microphone #{target} — see `vani mic list`", file=sys.stderr)
            return 1
        target, _label, bt = choices[idx]
    else:
        for name, _label, cand_bt in choices:
            if name == target:
                bt = cand_bt
                break
    if bt is not None:
        # The mic only exists in the headset profile; switch, then find the
        # real source name to persist (its suffix names the profile).
        card, profile = bt
        if not audio.ensure_source(target):
            print(f"error: could not activate the mic profile on {card}",
                  file=sys.stderr)
            return 1
        mac = card[len("bluez_card."):]
        target = next((n for n, _d in audio.list_sources()
                       if n.startswith(f"bluez_source.{mac}")), target)

    config.set_key("recording", "device", target, args.config)
    print(f"microphone: {target or 'system default'}")
    if service.restart_daemon():
        print("daemon restarted")
    else:
        print("restart the daemon to apply: vani service restart")
    return 0


def _mic_test(cfg: Config) -> int:
    """Record three seconds from the configured mic and transcribe it —
    one command that answers 'is the microphone actually working'."""
    from . import audio
    from .stream import LiveStream, StreamError

    device = cfg.recording.device
    print(f"recording 3s from {device or 'system default (%s)' % audio.default_source()}"
          " — say something...")
    mic = audio.Microphone(cfg.recording.sample_rate,
                           int(0.2 * cfg.recording.sample_rate * audio.SAMPLE_WIDTH),
                           device or None)
    stream = LiveStream(cfg)
    stream.start()
    total = 0
    peak_level = 0.0
    mic.open()
    try:
        for chunk in mic.chunks():
            stream.send(chunk)
            peak_level = max(peak_level, audio.rms(chunk))
            total += len(chunk)
            if total >= 3 * cfg.recording.sample_rate * audio.SAMPLE_WIDTH:
                break
    finally:
        mic.close()
    if total == 0:
        print("error: the microphone produced no audio at all", file=sys.stderr)
        stream.abort()
        return 1
    print(f"peak level: {peak_level:.0f} (speech is usually well above 500)")
    try:
        text = stream.finish(cfg.server.timeout_sec)
    except StreamError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"heard: {text}" if text else
          "heard: (nothing — wrong mic, muted, or you were quiet)")
    return 0


def cmd_say(args: argparse.Namespace) -> int:
    from . import audio
    from .notify import NullNotifier
    from .output import Typist
    from .stream import LiveStream, StreamError

    cfg = _load(args)
    try:
        pcm = audio.read_wav(args.file, cfg.recording.sample_rate)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    stream = LiveStream(cfg)
    stream.start()
    chunk_bytes = int(0.2 * cfg.recording.sample_rate * audio.SAMPLE_WIDTH)
    for i in range(0, len(pcm), chunk_bytes):
        stream.send(pcm[i:i + chunk_bytes])
    try:
        # The model may still be behind the audio; allow for the clip length.
        text = stream.finish(cfg.server.timeout_sec
                             + audio.duration(pcm, cfg.recording.sample_rate))
    except StreamError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not text:
        print("(no speech detected)", file=sys.stderr)
        return 1
    print(text)
    if args.type:
        from .daemon import deliver

        deliver(text, Typist(cfg.output.typer, cfg.output.type_delay_ms),
                NullNotifier(), cfg)
    return 0


# --------------------------------------------------------------------------


def _load(args: argparse.Namespace, required: bool = True) -> Config:
    try:
        return config.load(args.config, required=required)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
