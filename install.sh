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
if command -v pipx >/dev/null; then
    pipx install --force .
else
    python3 -m pip install --user --upgrade .
fi
export PATH="$PIP_USER_BIN:$PATH"
command -v vani >/dev/null || warn "$PIP_USER_BIN is not on your PATH — add it to ~/.profile"

say "Installing the wake-word engine (optional)"
if python3 -c 'import vosk' 2>/dev/null; then
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
    echo "  tray (optional):  systemctl --user enable --now vani-tray.service"
fi

say "Checking the installation"
vani doctor || true

cat <<'EOF'

Next steps
  1. Put your API token in ~/.config/vani/config.toml (server.token).
  2. Restart the daemon:  systemctl --user restart vani-daemon
  3. Say the wake word, or press the media key, and start talking.

  vani status    what the daemon is doing
  vani doctor    diagnose problems
  vani history   past transcripts
EOF
