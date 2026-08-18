#!/usr/bin/env bash
# Remove vani from this machine: services stopped and disabled, units removed,
# package uninstalled, runtime state cleared.
#
#   ./uninstall.sh           keep the config, history, and wake-word model
#   ./uninstall.sh --purge   remove those too (everything vani ever wrote)
#
# Deliberately not `set -e`: an uninstall must plough through a partial or
# broken install rather than stop at the first thing that was already gone.
set -uo pipefail

PURGE=0
for arg in "$@"; do
    case "$arg" in
        --purge)   PURGE=1 ;;
        -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

say "Stopping and disabling services"
if command -v systemctl >/dev/null; then
    systemctl --user disable --now vani-daemon.service vani-tray.service \
        ydotoold.service 2>/dev/null
    rm -f "$HOME/.config/systemd/user/vani-daemon.service" \
          "$HOME/.config/systemd/user/vani-tray.service" \
          "$HOME/.config/systemd/user/ydotoold.service"
    systemctl --user daemon-reload
    echo "  services stopped, units removed"
fi
# A daemon started by hand has no unit; vani quit finds it by pidfile.
command -v vani >/dev/null && vani quit 2>/dev/null

say "Removing the package"
if command -v pipx >/dev/null && pipx list 2>/dev/null | grep -q "package vani"; then
    pipx uninstall vani
else
    python3 -m pip uninstall -y vani 2>/dev/null || echo "  (was not pip-installed)"
fi

say "Clearing runtime state"
rm -rf "${XDG_RUNTIME_DIR:-/tmp/vani-$(id -u)}/vani"
echo "  done"

if ((PURGE)); then
    say "Purging configuration and data"
    rm -rf "$HOME/.config/vani" "$HOME/.cache/vani" "$HOME/.local/share/vani"
    echo "  removed ~/.config/vani ~/.cache/vani ~/.local/share/vani"
else
    say "Kept (rerun with --purge to remove)"
    echo "  ~/.config/vani        configuration"
    echo "  ~/.cache/vani         transcript history"
    echo "  ~/.local/share/vani   wake-word model"
fi

printf '\nvani is uninstalled.\n'
