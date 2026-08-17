"""Optional tray indicator.

Shows what the daemon is doing and keeps the last few transcripts a click
away. Purely cosmetic: dictation works whether or not this is running, and it
holds no state of its own — everything comes from the status file and the
history log.

Needs PyGObject with the AppIndicator3 typelib, which is a system package
(`gir1.2-appindicator3-0.1`) rather than something pip can install; the import
error below says so rather than dumping a traceback.
"""
from __future__ import annotations

import subprocess
import sys

from . import paths, state

ICONS = {
    state.IDLE: "audio-input-microphone-symbolic",
    state.RECORDING: "media-record-symbolic",
    state.SILENCE: "appointment-soon-symbolic",
    state.TRANSCRIBING: "emblem-synchronizing-symbolic",
}
LABELS = {
    state.IDLE: "Idle — press the key or say the wake word",
    state.RECORDING: "● Recording... (pause to send)",
    state.SILENCE: "Sending soon — speak to continue",
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
            self.menu = Gtk.Menu()
            self.ind.set_menu(self.menu)
            self.rebuild(state.IDLE)
            GLib.timeout_add(500, self.poll)

        def poll(self) -> bool:
            current, _ = state.read_status()
            if current != self.state:
                self.state = current
                self.ind.set_icon_full(ICONS[current], current)
                self.rebuild(current)
            return True

        def rebuild(self, current: str) -> None:
            for child in self.menu.get_children():
                self.menu.remove(child)

            head = Gtk.MenuItem(label=LABELS[current])
            head.set_sensitive(False)
            self.menu.append(head)

            toggle = Gtk.MenuItem(
                label="Stop & transcribe" if current == state.RECORDING
                else "Start dictation")
            toggle.connect("activate", lambda *_: self.run_toggle())
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
                empty = Gtk.MenuItem(label="(no transcripts yet)")
                empty.set_sensitive(False)
                self.menu.append(empty)

            self.menu.append(Gtk.SeparatorMenuItem())
            quit_item = Gtk.MenuItem(label="Quit tray")
            quit_item.connect("activate", Gtk.main_quit)
            self.menu.append(quit_item)
            self.menu.show_all()

        def run_toggle(self) -> None:
            subprocess.Popen([sys.argv[0], "toggle"]
                             if sys.argv[0].endswith("vani")
                             else [sys.executable, "-m", "vani", "toggle"])

        def copy(self, _widget, text: str) -> None:
            clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
            clipboard.set_text(text, -1)
            clipboard.store()

    paths.ensure_dirs()
    Tray()
    Gtk.main()
    return 0
