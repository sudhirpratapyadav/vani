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
    entries = state.read_history(1)
    if entries:
        stamp, text = entries[0]
        print(f"last:   {text[:80]}" + (f"  ({stamp})" if stamp else ""))
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
    text = config.render(cfg)
    token = cfg.server.token
    if token:  # never print the secret, even to a terminal the user owns
        text = text.replace(token, "***")
    print(text)
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
    if not cfg.server.token:
        print("\nNext: put your API token in that file (server.token), then run "
              "`vani doctor`.")
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


def cmd_say(args: argparse.Namespace) -> int:
    from . import audio
    from .client import Client, TranscribeError
    from .notify import NullNotifier
    from .output import Typist

    cfg = _load(args)
    try:
        pcm = audio.read_wav(args.file, cfg.recording.sample_rate)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if cfg.recording.auto_gain:
        pcm, _ = audio.auto_gain(pcm)
    try:
        text = Client(cfg).transcribe(audio.to_wav(pcm, cfg.recording.sample_rate))
    except (TranscribeError, ConfigError) as exc:
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
