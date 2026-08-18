"""The tray indicator.

Shows what the daemon is doing, whether the server is reachable, and keeps
the last few transcripts a click away. Dictation works whether or not this is
running, and it holds no state of its own — everything comes from the status
files and the history log, so it never disagrees with the daemon for long.

The menu also carries the app-level controls a desktop app is expected to
have: a Settings submenu (start on login, the config file, restarting the
daemon) and Quit, which stops the whole app — daemon included — not just the
indicator.

Needs PyGObject with the AppIndicator3 typelib, which is a system package
(`gir1.2-appindicator3-0.1`) rather than something pip can install; the import
error below says so rather than dumping a traceback.
"""
from __future__ import annotations

import subprocess
import sys
import threading

from . import paths, service, state

ICONS = {
    state.IDLE: "audio-input-microphone-symbolic",
    state.RECORDING: "media-record-symbolic",
    state.SILENCE: "appointment-soon-symbolic",
    state.TRANSCRIBING: "emblem-synchronizing-symbolic",
}
LABELS = {
    state.IDLE: "Idle — press the key or say the wake word",
    state.RECORDING: "● Recording... (pause to finish)",
    state.SILENCE: "Typing soon — speak to continue",
    state.TRANSCRIBING: "Transcribing...",
}
MAX_ITEM_CHARS = 60
RECENT_ITEMS = 5


def run() -> int:
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("AppIndicator3", "0.1")
        from gi.repository import AppIndicator3, Gdk, GLib, Gtk
    except (ImportError, ValueError) as exc:
        print("the tray needs PyGObject and AppIndicator3:\n"
              "  sudo apt install python3-gi gir1.2-appindicator3-0.1\n"
              "and the 'Ubuntu AppIndicators' GNOME extension.\n"
              f"({exc})", file=sys.stderr)
        return 1

    class Tray:
        def __init__(self) -> None:
            self.ind = AppIndicator3.Indicator.new(
                "vani", ICONS[state.IDLE],
                AppIndicator3.IndicatorCategory.APPLICATION_STATUS)
            self.ind.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
            self.ind.set_title("vani")
            self.state = None
            self.server = ("unpolled", "")
            self.menu = Gtk.Menu()
            self.ind.set_menu(self.menu)
            self.rebuild(state.IDLE)
            GLib.timeout_add(500, self.poll)

        def poll(self) -> bool:
            current, _ = state.read_status()
            server = state.read_server()
            if current != self.state or server != self.server:
                self.state, self.server = current, server
                self.ind.set_icon_full(ICONS[current], current)
                self.rebuild(current)
            return True

        # -- menu ----------------------------------------------------------

        def rebuild(self, current: str) -> None:
            for child in self.menu.get_children():
                self.menu.remove(child)

            self._append_label(LABELS[current])
            self._append_label(self._server_label())

            toggle = Gtk.MenuItem(
                label="Stop & transcribe" if current == state.RECORDING
                else "Start dictation")
            toggle.connect("activate", lambda *_: self.run_vani("toggle"))
            self.menu.append(toggle)

            self.menu.append(Gtk.SeparatorMenuItem())
            entries = state.read_history(RECENT_ITEMS)
            if entries:
                for _stamp, text in entries:
                    label = (text if len(text) <= MAX_ITEM_CHARS
                             else text[: MAX_ITEM_CHARS - 3] + "...")
                    item = Gtk.MenuItem(label=label)
                    item.connect("activate", self.copy, text)
                    self.menu.append(item)
                full = Gtk.MenuItem(label="Open full history")
                full.connect("activate", lambda *_: subprocess.Popen(
                    ["xdg-open", str(paths.history_file())]))
                self.menu.append(full)
            else:
                self._append_label("(no transcripts yet)")

            self.menu.append(Gtk.SeparatorMenuItem())
            settings = Gtk.MenuItem(label="Settings")
            settings.set_submenu(self._settings_menu())
            self.menu.append(settings)

            quit_item = Gtk.MenuItem(label="Quit vani")
            quit_item.connect("activate", self.quit_everything)
            self.menu.append(quit_item)
            self.menu.show_all()

        def _settings_menu(self) -> "Gtk.Menu":
            sub = Gtk.Menu()

            mic = Gtk.MenuItem(label="Microphone")
            mic.set_submenu(self._mic_menu())
            sub.append(mic)

            login = Gtk.CheckMenuItem(label="Start on login")
            login.set_active(service.starts_on_login())
            # Connected after set_active, so building the menu never toggles.
            login.connect("toggled", lambda item: threading.Thread(
                target=service.set_start_on_login, args=(item.get_active(),),
                daemon=True).start())
            sub.append(login)

            edit = Gtk.MenuItem(label="Edit settings file")
            edit.connect("activate", lambda *_: subprocess.Popen(
                ["xdg-open", str(paths.config_file())]))
            sub.append(edit)

            restart = Gtk.MenuItem(label="Restart daemon")
            restart.connect("activate", lambda *_: subprocess.Popen(
                ["systemctl", "--user", "restart", service.DAEMON_UNIT]))
            sub.append(restart)

            check = Gtk.MenuItem(label="Check server now")
            check.connect("activate", lambda *_: threading.Thread(
                target=self.check_server, daemon=True).start())
            sub.append(check)
            return sub

        def _mic_menu(self) -> "Gtk.Menu":
            """Radio list of inputs; selecting one runs `vani mic set`, which
            owns the persistence, Bluetooth profile switch, and daemon restart."""
            from .cli import mic_choices
            from .config import load

            sub = Gtk.Menu()
            try:
                current = load(required=False).recording.device
            except Exception:
                current = ""

            def add(label: str, value: str, active: bool) -> None:
                item = Gtk.CheckMenuItem(label=label)
                item.set_draw_as_radio(True)
                item.set_active(active)
                # Connected after set_active, so building never triggers it.
                item.connect("activate",
                             lambda *_: self.run_vani("mic", "set", value))
                sub.append(item)

            add("System default", "default", not current)
            try:
                for name, label, bt in mic_choices():
                    active = bool(current) and (current == name
                                                or current.startswith(name))
                    add(label, name, active)
            except Exception:
                pass  # no pactl: the default entry alone is still truthful
            return sub

        def _server_label(self) -> str:
            ok, detail = self.server if self.server != ("unpolled", "") \
                else state.read_server()
            if ok is None:
                return "Server: not checked yet"
            return "Server: online" if ok else "Server: DOWN — " + (detail or "?")

        def _append_label(self, text: str) -> None:
            item = Gtk.MenuItem(label=text)
            item.set_sensitive(False)
            self.menu.append(item)

        # -- actions --------------------------------------------------------

        def run_vani(self, *args: str) -> None:
            subprocess.Popen([sys.argv[0], *args]
                             if sys.argv[0].endswith("vani")
                             else [sys.executable, "-m", "vani", *args])

        def check_server(self) -> None:
            """Off the GTK thread: probe health and publish the verdict."""
            from .client import ServerError, check_health
            from .config import ConfigError, load

            try:
                check_health(load(required=False))
                state.set_server(True)
            except (ServerError, ConfigError) as exc:
                state.set_server(False, str(exc))
            # poll() sees the changed state file and rebuilds the menu.

        def quit_everything(self, *_args) -> None:
            """Stop the daemon and the services, then leave ourselves."""
            for line in service.quit_all():
                print(line, flush=True)
            Gtk.main_quit()

        def copy(self, _widget, text: str) -> None:
            clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
            clipboard.set_text(text, -1)
            clipboard.store()

    paths.ensure_dirs()
    Tray()
    Gtk.main()
    return 0
