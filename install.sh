#!/usr/bin/env bash
# Install vani for the current user: the package, the config, the wake-word
# model, and the systemd user services. Everything lands under $HOME; nothing
# here needs root except the optional apt step, which it only suggests.
#
#   ./install.sh              full install
#   ./install.sh --no-model   skip the 40 MB wake-word model
#   ./install.sh --no-service skip the systemd units
set -euo pipefail

cd "$(dirname "$0")"

WITH_MODEL=1
WITH_SERVICE=1
for arg in "$@"; do
    case "$arg" in
        --no-model)   WITH_MODEL=0 ;;
        --no-service) WITH_SERVICE=0 ;;
        -h|--help)    sed -n '2,9p' "$0"; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m  ! %s\033[0m\n' "$*"; }

# ---------------------------------------------------------------- packages
say "Checking system packages"
missing=()
for bin in arecord xdotool xinput; do
    command -v "$bin" >/dev/null || missing+=("$bin")
done
if ((${#missing[@]})); then
    warn "missing: ${missing[*]}"
    warn "install with: sudo apt install alsa-utils xdotool xinput"
fi

# ----------------------------------------------------------------- install
say "Installing the vani package"
PIP_USER_BIN="$HOME/.local/bin"
export PATH="$PIP_USER_BIN:$PATH"
WAKE_INSTALLED=0

if command -v pipx >/dev/null; then
    # The extra has to go in the same venv: a `pip install --user vosk` is not
    # importable from inside a pipx-managed environment.
    if pipx install --force ".[wake]"; then
        WAKE_INSTALLED=1
    else
        warn "the wake-word extra failed to build — installing without it"
        pipx install --force .
    fi
elif python3 -m venv "${BUILD_DIR:=$(mktemp -d)}/venv" 2>/dev/null; then
    # Not `pip install --user .`: Ubuntu 22.04's system pip puts
    # /usr/lib/python3/dist-packages (setuptools 59, which predates PEP 621)
    # ahead of the setuptools it fetches for the build, so [project] is ignored
    # and the result is an UNKNOWN-0.0.0 package with no `vani` command and no
    # warning. Building the wheel in a throwaway venv gets a setuptools new
    # enough to read pyproject.toml.
    trap 'rm -rf "$BUILD_DIR"' EXIT
    "$BUILD_DIR/venv/bin/pip" -q install --upgrade pip setuptools wheel
    "$BUILD_DIR/venv/bin/pip" -q wheel --no-deps -w "$BUILD_DIR/dist" .
    python3 -m pip install --user --force-reinstall --no-deps "$BUILD_DIR"/dist/vani-*.whl
    # --no-deps skipped the one hard dependency; install it explicitly.
    python3 -m pip install --user 'websockets>=12'
else
    warn "python3-venv is missing — falling back to a direct pip install"
    warn "if that yields 'UNKNOWN-0.0.0': sudo apt install python3-venv, then rerun"
    python3 -m pip install --user --upgrade .
fi

command -v vani >/dev/null \
    || warn "no vani command after install — is $PIP_USER_BIN on your PATH?"

say "Installing the wake-word engine (optional)"
if ((WAKE_INSTALLED)); then
    echo "  installed alongside vani"
elif python3 -c 'import vosk' 2>/dev/null; then
    echo "  vosk already installed"
else
    python3 -m pip install --user vosk || warn "vosk install failed — hotkey dictation still works"
fi

# ------------------------------------------------------------------ config
say "Configuration"
if [[ -f "$HOME/.config/vani/config.toml" ]]; then
    echo "  keeping existing $HOME/.config/vani/config.toml"
else
    vani config init
fi

# ------------------------------------------------------------------- model
if ((WITH_MODEL)); then
    say "Wake-word model"
    vani model download || warn "model download failed — retry later with: vani model download"
fi

# ---------------------------------------------------------------- services
if ((WITH_SERVICE)); then
    say "systemd user services"
    units="$HOME/.config/systemd/user"
    mkdir -p "$units"
    cp systemd/vani-daemon.service systemd/vani-tray.service "$units/"
    systemctl --user daemon-reload
    # Hand the units the X credentials the running session already has.
    systemctl --user import-environment DISPLAY XAUTHORITY 2>/dev/null || true
    systemctl --user enable --now vani-daemon.service
    echo "  enabled vani-daemon.service"
    # The tray is optional, but "optional" was being read as "someone will
    # enable this later" and nobody did — so turn it on when the typelib it
    # needs is actually installed, and explain the gap when it is not.
    if python3 -c 'import gi; gi.require_version("AppIndicator3", "0.1")' 2>/dev/null; then
        systemctl --user enable --now vani-tray.service
        echo "  enabled vani-tray.service"
    else
        warn "no AppIndicator3 typelib — skipping the tray; to enable it later:"
        warn "  sudo apt install python3-gi gir1.2-appindicator3-0.1"
        warn "  systemctl --user enable --now vani-tray.service"
    fi
fi

say "Checking the installation"
vani doctor || true

cat <<'EOF'

vani is installed. Say the wake word ("hey claude"), or press the media key,
and start talking — the text appears live and is typed when you pause.

  vani status     what the daemon is doing, and server connectivity
  vani doctor     diagnose problems
  vani service    start/stop/restart, or enable/disable start on login
  vani quit       stop everything (also in the tray menu)
  ./uninstall.sh  remove vani (--purge also deletes config and history)
EOF
