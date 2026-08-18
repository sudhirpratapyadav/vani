"""The desktop UI process: tray indicator plus the live-caption overlay.

The tray shows what the daemon is doing, whether the server is reachable,
and keeps the last few transcripts a click away. The overlay is a small
translucent always-on-top window that appears while recording and shows the
transcript forming in real time — a notification is the wrong surface for
that: it truncates, and the countdown kept overwriting the words.

Neither holds state of its own — everything comes from the runtime files the
daemon writes (status, live text, server verdict), polled a few times a
second, so this process can die and restart without anyone noticing. Its
pidfile is how the daemon knows the overlay is there (and that it can stop
mirroring live text into notifications).

Needs PyGObject with the AppIndicator3 typelib, which is a system package
(`gir1.2-appindicator3-0.1`) rather than something pip can install; the import
error below says so rather than dumping a traceback. On Wayland the process
runs on the X11 backend (XWayland) so the overlay can actually be positioned —
Wayland gives clients no say in window placement.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

from . import paths, service, state
from .config import load as load_config
from .output import session_type

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
#: How long the overlay lingers on the finished transcript.
LINGER_SEC = 2.0


def run() -> int:
    if session_type() == "wayland" and os.environ.get("DISPLAY"):
        # Wayland never lets a client place its own window; XWayland does.
        os.environ.setdefault("GDK_BACKEND", "x11")
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

    try:
        cfg = load_config(required=False)
    except Exception:
        from .config import Config

        cfg = Config()

    class Overlay:
        """The live-caption window: fixed width, grows until max_height,
        then scrolls so the newest words stay in view."""

        def __init__(self) -> None:
            ui = cfg.ui
            self.win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
            w = self.win
            w.set_decorated(False)
            w.set_resizable(False)
            w.set_skip_taskbar_hint(True)
            w.set_skip_pager_hint(True)
            w.set_keep_above(True)
            w.set_accept_focus(False)
            w.set_focus_on_map(False)
            w.set_type_hint(Gdk.WindowTypeHint.NOTIFICATION)
            w.set_name("vani-overlay")

            screen = w.get_screen()
            visual = screen.get_rgba_visual()
            if visual is not None:
                w.set_visual(visual)
            w.set_app_paintable(True)
            css = Gtk.CssProvider()
            css.load_from_data(f"""
                #vani-overlay {{
                    background-color: rgba(16, 18, 26, {ui.opacity});
                    border-radius: 14px;
                }}
                #vani-head {{ color: #8fb8e8; font-size: 12px; }}
                #vani-text {{ color: #f2f2f2; font-size: 15px; }}
            """.encode())
            Gtk.StyleContext.add_provider_for_screen(
                screen, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            for setter in (box.set_margin_top, box.set_margin_bottom,
                           box.set_margin_start, box.set_margin_end):
                setter(12)
            self.head = Gtk.Label(xalign=0)
            self.head.set_name("vani-head")
            self.text = Gtk.Label(xalign=0)
            self.text.set_name("vani-text")
            self.text.set_line_wrap(True)
            self.text.set_xalign(0)
            self.text.set_yalign(0)
            self.text.set_size_request(ui.width - 24, -1)

            self.scroll = Gtk.ScrolledWindow()
            self.scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            self.scroll.set_max_content_height(ui.max_height)
            self.scroll.add(self.text)

            box.pack_start(self.head, False, False, 0)
            box.pack_start(self.scroll, True, True, 0)
            w.add(box)
            w.set_default_size(ui.width, -1)
            w.connect("size-allocate", self._reposition)
            self.visible = False
            self._shown_text = None

        # -- geometry ------------------------------------------------------

        def _reposition(self, _w=None, alloc=None) -> None:
            """Bottom-center of the primary monitor, re-anchored as it grows."""
            display = Gdk.Display.get_default()
            monitor = display.get_primary_monitor() or display.get_monitor(0)
            if monitor is None:
                return
            geo = monitor.get_geometry()
            width, height = self.win.get_size()
            self.win.move(geo.x + (geo.width - width) // 2,
                          geo.y + geo.height - height - 72)

        def _scroll_to_end(self) -> bool:
            adj = self.scroll.get_vadjustment()
            adj.set_value(adj.get_upper() - adj.get_page_size())
            return False

        # -- content -------------------------------------------------------

        def update(self, status: str, countdown: float, live: str) -> None:
            head = {
                state.RECORDING: "●  listening",
                state.SILENCE: "⏸  typing in %.0fs — speak to continue" % countdown,
                state.TRANSCRIBING: "…  finishing",
                state.IDLE: "✓  typed",
            }[status]
            self.head.set_text(head)
            if live != self._shown_text:
                if not live:
                    # A new recording: collapse back to one line before the
                    # window regrows with the incoming text.
                    self.win.resize(cfg.ui.width, 1)
                self._shown_text = live
                self.text.set_text(live or "…")
                # A wrapped label does not propagate its height on its own;
                # measure it for our width and pin the scroller, capped at
                # max_height — past the cap, scrolling takes over.
                height = self.text.get_preferred_height_for_width(
                    cfg.ui.width - 24)[1]
                self.scroll.set_min_content_height(
                    min(height, cfg.ui.max_height))
                GLib.idle_add(self._scroll_to_end)
            if not self.visible:
                self.visible = True
                self.win.show_all()
                self._reposition()

        def hide(self) -> None:
            if self.visible:
                self.visible = False
                self._shown_text = None
                self.win.hide()
                self.win.resize(cfg.ui.width, 1)

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
            self.overlay = Overlay() if cfg.ui.enabled else None
            self._hide_at: float | None = None
            self.rebuild(state.IDLE)
            GLib.timeout_add(500, self.poll)
            GLib.timeout_add(120, self.tick_overlay)

        # -- polling -------------------------------------------------------

        def poll(self) -> bool:
            current, _ = state.read_status()
            server = state.read_server()
            if current != self.state or server != self.server:
                self.state, self.server = current, server
                self.ind.set_icon_full(ICONS[current], current)
                self.rebuild(current)
            return True

        def tick_overlay(self) -> bool:
            if self.overlay is None:
                return True
            status, countdown = state.read_status()
            if status != state.IDLE:
                self._hide_at = None
                self.overlay.update(status, countdown, state.read_live())
            elif self.overlay.visible:
                live = state.read_live()
                if not live:
                    self.overlay.hide()  # failed or cancelled: nothing to show
                elif self._hide_at is None:
                    self._hide_at = time.time() + LINGER_SEC
                    self.overlay.update(state.IDLE, 0.0, live)
                elif time.time() >= self._hide_at:
                    self._hide_at = None
                    self.overlay.hide()
            return True

        # -- menu ----------------------------------------------------------

        def rebuild(self, current: str) -> None:
            for child in self.menu.get_children():
                self.menu.remove(child)

            self._append_label(LABELS[current])
            self._append_label(self._server_label())

            toggle = Gtk.MenuItem(
                label="Stop & type" if current in (state.RECORDING, state.SILENCE)
                else "Start dictation")
            toggle.connect("activate", lambda *_: self.run_vani("toggle"))
            self.menu.append(toggle)

            if current in (state.RECORDING, state.SILENCE):
                cancel = Gtk.MenuItem(label="Cancel (discard)")
                cancel.connect("activate", lambda *_: self.run_vani("cancel"))
                self.menu.append(cancel)

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

            sub = Gtk.Menu()
            try:
                current = load_config(required=False).recording.device
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
            from .config import ConfigError

            try:
                check_health(load_config(required=False))
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
    state.write_pidfile(paths.tray_pidfile())
    try:
        Tray()
        Gtk.main()
    finally:
        state.clear_pidfile(paths.tray_pidfile())
    return 0
