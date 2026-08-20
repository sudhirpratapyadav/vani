"""The desktop UI process: the pill instrument plus the tray indicator.

One surface owns the session. The pill is a small always-on-top window that
morphs through the states of a recording — a dim idle dot (amber when the
server is down), a red dot with a live waveform while listening, a draining
ring during the silence countdown, a shimmer while finishing, a green
"✓ typed → window" flash, a red error card with a retry button. Motion and
color carry the state so peripheral vision can read it; words appear only in
the optional caption card that grows out of the pill.

Everything arrives over the daemon's event bus (events.py) — including the
8 Hz microphone level that drives the waveform, which is the live answer to
"is it hearing me?". While the bus is unreachable (old daemon, daemon
restarting) the pill degrades to polling the status files, coarse but honest.

The tray mirrors the pill's state in a runtime-generated mic glyph (no more
borrowed GNOME icons) and holds the launcher menu: toggle, enable/disable,
recent transcripts, settings. It shows nothing the pill already said.

Needs PyGObject with the AppIndicator3 typelib, which is a system package
(`gir1.2-appindicator3-0.1`) rather than something pip can install; the import
error below says so rather than dumping a traceback. On Wayland the process
runs on the X11 backend (XWayland) so the pill can actually be positioned —
Wayland gives clients no say in window placement.
"""
from __future__ import annotations

import math
import os
import subprocess
import sys
import threading
import time

from . import config as config_mod
from . import daemon as daemon_mod
from . import paths, service, state
from .config import UI_CAPTIONS, UI_POSITIONS, load as load_config
from .events import EventClient
from .output import focused_center, session_type

MAX_ITEM_CHARS = 60
RECENT_ITEMS = 5
#: How long the caption card lingers on the finished transcript.
LINGER_SEC = 2.0
#: Transient pill states and their time on stage.
TYPED_SEC = 1.6
CANCELLED_SEC = 1.4
ERROR_SEC = 8.0
BLOCKED_SEC = 4.0

# The instrument's palette (see docs/ux-mockup.html).
COLORS = {
    "ink": (0.91, 0.92, 0.95),
    "muted": (0.55, 0.58, 0.65),
    "dim": (0.34, 0.38, 0.47),
    "accent": (0.56, 0.72, 0.91),
    "rec": (1.00, 0.36, 0.41),
    "proc": (0.42, 0.64, 1.00),
    "ok": (0.30, 0.83, 0.49),
    "warn": (0.96, 0.71, 0.33),
}
#: Grow/shrink tween. Short enough to feel instant, long enough to read as
#: motion in peripheral vision — which is the whole point of the gesture.
SPRING_SEC = 0.17
PILL_BG = (0.055, 0.063, 0.086)
PILL_H = 38
DOT_WIN = 18
WAVE_BARS = 18

#: Tray icon color per coarse state.
ICON_STATES = {
    "asleep": COLORS["accent"],
    "blocked": COLORS["warn"],
    "rec": COLORS["rec"],
    "busy": COLORS["proc"],
    "off": COLORS["dim"],
}


def run() -> int:
    if session_type() == "wayland" and os.environ.get("DISPLAY"):
        # Wayland never lets a client place its own window; XWayland does.
        os.environ.setdefault("GDK_BACKEND", "x11")
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("AppIndicator3", "0.1")
        gi.require_version("PangoCairo", "1.0")
        from gi.repository import AppIndicator3, Gdk, GLib, Gtk, Pango, PangoCairo
        import cairo
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

    # ------------------------------------------------------------------
    # Cairo helpers

    def rounded_rect(cr, x, y, w, h, r) -> None:
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()

    def set_color(cr, name_or_rgb, alpha=1.0) -> None:
        rgb = COLORS.get(name_or_rgb, name_or_rgb) \
            if isinstance(name_or_rgb, str) else name_or_rgb
        cr.set_source_rgba(rgb[0], rgb[1], rgb[2], alpha)

    def text_layout(cr, text, size=10.5):
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(
            Pango.FontDescription(f"Cantarell, Sans {size}"))
        layout.set_text(text, -1)
        return layout

    def ease_out_back(t: float) -> float:
        """Overshoot slightly, then settle — the spring in "spring animation"."""
        t -= 1.0
        return t * t * (2.70158 * t + 1.70158) + 1.0

    def measure_text(text, size=10.5) -> int:
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)
        cr = cairo.Context(surface)
        layout = text_layout(cr, text, size)
        return layout.get_pixel_size()[0]

    #: Cached monitor geometry, so positioning never shells out mid-animation.
    _monitor_geo = [None]

    def active_monitor(refresh: bool = False):
        """Geometry of the monitor the user is actually working on.

        Resolved from the focused window (X11) and cached: `_position` runs on
        every animation frame, and an xdotool call per frame would cost more
        than the animation it is placing. Refreshed when a session begins,
        which is the only moment the answer can matter.
        """
        display = Gdk.Display.get_default()
        if display is None:
            return None
        if refresh or _monitor_geo[0] is None:
            monitor = None
            center = focused_center()
            if center is not None:
                monitor = display.get_monitor_at_point(*center)
            if monitor is None:
                monitor = display.get_primary_monitor() or display.get_monitor(0)
            _monitor_geo[0] = monitor.get_geometry() if monitor else None
        return _monitor_geo[0]

    # ------------------------------------------------------------------
    # The pill

    class Pill:
        """One window, many shapes. All mutation happens on the GTK thread."""

        def __init__(self) -> None:
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
            visual = w.get_screen().get_rgba_visual()
            if visual is not None:
                w.set_visual(visual)
            w.set_app_paintable(True)
            w.add_events(Gdk.EventMask.BUTTON_PRESS_MASK
                         | Gdk.EventMask.ENTER_NOTIFY_MASK
                         | Gdk.EventMask.LEAVE_NOTIFY_MASK)
            w.connect("draw", self._draw)
            w.connect("button-press-event", self._click)
            w.connect("enter-notify-event", self._crossing, True)
            w.connect("leave-notify-event", self._crossing, False)
            #: Set by the tray; called with True/False as the pointer
            #: enters and leaves, to drive `ui.captions = "hover"`.
            self.on_hover = lambda _inside: None
            self.hovered = False

            #: Render state: hidden | dot | listening | countdown | finishing
            #: | transient (typed/copied/cancelled/error carried in _transient)
            self.mode = "hidden"
            self.server_ok: "bool | None" = None
            self.levels: "list[tuple[float, float]]" = []  # (rms, threshold)
            self.countdown = 0.0
            self.countdown_at = 0.0
            self.slow = False
            self.finish_at = 0.0
            self.ptt = False
            self._spring_from = (0, 0)
            self._spring_to_size = (0, 0)
            self._spring_at = 0.0
            self._springing = False
            self._transient: "dict | None" = None
            self._transient_seq = 0
            self._anim_running = False
            self._regions: "dict[str, tuple[int, int, int, int]]" = {}
            self._size = (0, 0)
            self.visible = False

        # -- state entry points (GTK thread) ---------------------------

        def show_mode(self, mode: str) -> None:
            if self._transient is not None and mode in ("dot", "hidden"):
                return  # the transient finishes its say first
            if mode == self.mode and mode in ("dot", "hidden"):
                return  # the poll fallback re-asserts idle twice a second
            previous, self.mode = self.mode, mode
            if mode == "listening":
                self.ptt = False
            if mode == "finishing" and previous != "finishing":
                self.finish_at = time.monotonic()
            self._apply()

        def show_transient(self, kind: str, text: str, seconds: float,
                           retry: bool = False) -> None:
            self._transient_seq += 1
            seq = self._transient_seq
            self._transient = {"kind": kind, "text": text, "retry": retry}
            self.mode = "transient"
            self._apply()
            GLib.timeout_add(int(seconds * 1000), self._expire, seq)

        def _expire(self, seq: int) -> bool:
            if seq == self._transient_seq and self._transient is not None:
                self._transient = None
                if self.mode == "transient":
                    self.mode = "dot"
                self._apply()
            return False

        def dismiss(self) -> None:
            self._transient_seq += 1
            self._transient = None
            if self.mode == "transient":
                self.mode = "dot"
            self._apply()

        def add_level(self, rms: float, threshold: float) -> None:
            self.levels.append((rms, threshold))
            del self.levels[:-WAVE_BARS - 4]
            if self.mode in ("listening", "countdown"):
                self.win.queue_draw()

        def set_countdown(self, seconds: float) -> None:
            self.countdown = seconds
            self.countdown_at = time.monotonic()

        # -- geometry ---------------------------------------------------

        def _wanted_size(self) -> "tuple[int, int]":
            if self.mode == "dot":
                return (DOT_WIN, DOT_WIN)
            if self.mode in ("listening", "countdown"):
                return (196, PILL_H)
            if self.mode == "finishing":
                if self.slow:
                    # Sized for the widest counter it will ever show, so the
                    # pill does not twitch wider on every passing second.
                    return (200 + measure_text("taking longer than usual… 00s"),
                            PILL_H)
                return (168, PILL_H)
            if self.mode == "transient" and self._transient is not None:
                t = self._transient
                width = measure_text(t["text"]) + 46
                if t["retry"]:
                    width += 74
                return (max(150, min(width, 520)), PILL_H)
            return (DOT_WIN, DOT_WIN)

        def _apply(self) -> None:
            if self.mode == "hidden" or \
                    (self.mode == "dot" and not self._dot_visible()):
                if self.visible:
                    self.visible = False
                    self.win.hide()
                self._springing = False
                self._stop_anim()
                return
            size = self._wanted_size()
            if not self.visible:
                # Grow out of the dot rather than appearing at full width —
                # motion is what the eye catches at the edge of vision.
                self._size = (DOT_WIN, DOT_WIN)
                self.win.resize(*self._size)
                self._position(self._size)
                self.visible = True
                self.win.show_all()
            if size != self._size:
                self._spring_to(size)
            else:
                self._position(size)
            self.win.queue_draw()
            if self.mode in ("countdown", "finishing"):
                self._start_anim()
            elif not self._springing:
                self._stop_anim()

        # -- the spring -------------------------------------------------

        def _spring_to(self, size) -> None:
            self._spring_from = self._size
            self._spring_to_size = size
            self._spring_at = time.monotonic()
            if not self._springing:
                self._springing = True
                GLib.timeout_add(16, self._spring_tick)

        def _spring_tick(self) -> bool:
            if not self.visible:
                self._springing = False
                return False
            fraction = (time.monotonic() - self._spring_at) / SPRING_SEC
            target = self._spring_to_size
            if fraction >= 1.0:
                self._springing = False
                self._set_size(target)
                if self.mode not in ("countdown", "finishing"):
                    self._stop_anim()
                return False
            eased = ease_out_back(fraction)
            self._set_size((
                max(DOT_WIN, int(self._spring_from[0]
                                 + (target[0] - self._spring_from[0]) * eased)),
                max(DOT_WIN, int(self._spring_from[1]
                                 + (target[1] - self._spring_from[1]) * eased)),
            ))
            return True

        def _set_size(self, size) -> None:
            if size != self._size:
                self._size = size
                self.win.resize(*size)
            self._position(size)
            self.win.queue_draw()

        def _dot_visible(self) -> bool:
            return cfg.ui.idle_dot

        def _position(self, size) -> None:
            geo = active_monitor()
            if geo is None:
                return
            w, h = size
            pos = cfg.ui.position
            margin_v, margin_h = 72, 24
            if pos.endswith("left"):
                x = geo.x + margin_h
            elif pos.endswith("right"):
                x = geo.x + geo.width - w - margin_h
            else:
                x = geo.x + (geo.width - w) // 2
            if pos.startswith("top"):
                y = geo.y + margin_v
            else:
                y = geo.y + geo.height - h - margin_v
            self.win.move(x, y)

        # -- animation --------------------------------------------------

        def _start_anim(self) -> None:
            if not self._anim_running:
                self._anim_running = True
                GLib.timeout_add(45, self._animate)

        def _stop_anim(self) -> None:
            self._anim_running = False

        def _animate(self) -> bool:
            if not self._anim_running:
                return False
            self.win.queue_draw()
            return True

        # -- drawing ----------------------------------------------------

        def _draw(self, _w, cr) -> bool:
            cr.set_operator(cairo.OPERATOR_SOURCE)
            cr.set_source_rgba(0, 0, 0, 0)
            cr.paint()
            cr.set_operator(cairo.OPERATOR_OVER)
            self._regions = {}
            w, h = self._size
            if self.mode == "dot":
                # Amber when the server is down: the mic's honesty, visible
                # before anyone speaks. Dim accent otherwise.
                blocked = self.server_ok is False
                set_color(cr, "warn" if blocked else "accent",
                          0.85 if blocked else 0.55)
                cr.arc(w / 2, h / 2, 4.0 if blocked else 3.5, 0, 2 * math.pi)
                cr.fill()
                return True

            # Pill background, with a red edge only for the error card.
            rounded_rect(cr, 0, 0, w, h, h / 2)
            cr.set_source_rgba(*PILL_BG, cfg.ui.opacity)
            cr.fill()
            is_error = (self.mode == "transient" and self._transient is not None
                        and self._transient["kind"] == "error")
            rounded_rect(cr, 0.75, 0.75, w - 1.5, h - 1.5, (h - 1.5) / 2)
            set_color(cr, "rec" if is_error else (1, 1, 1),
                      0.45 if is_error else 0.10)
            cr.set_line_width(1.5)
            cr.stroke()

            if self.mode in ("listening", "countdown"):
                x = 16
                if self.mode == "listening":
                    set_color(cr, "rec")
                    cr.arc(x + 5, h / 2, 5, 0, 2 * math.pi)
                    cr.fill()
                    if self.ptt:
                        # Push-to-talk: a ring around the dot, the way a held
                        # key looks. Tells the user releasing will send now,
                        # rather than the three-second silence wait.
                        set_color(cr, "rec", 0.45)
                        cr.set_line_width(1.6)
                        cr.arc(x + 5, h / 2, 8.5, 0, 2 * math.pi)
                        cr.stroke()
                    x += 20
                else:
                    self._draw_ring(cr, x + 10, h / 2)
                    x += 30
                self._draw_wave(cr, x, w - x - 40, h)
                self._draw_cancel(cr, w - 26, h / 2)
            elif self.mode == "finishing":
                set_color(cr, "proc")
                cr.arc(16 + 5, h / 2, 5, 0, 2 * math.pi)
                cr.fill()
                x = 36
                wave_w = 100
                self._draw_wave(cr, x, wave_w, h, frozen=True)
                if self.slow:
                    waited = int(time.monotonic() - self.finish_at)
                    layout = text_layout(
                        cr, "taking longer than usual… %ds" % max(waited, 0))
                    set_color(cr, "muted")
                    cr.move_to(x + wave_w + 10,
                               (h - layout.get_pixel_size()[1]) / 2)
                    PangoCairo.show_layout(cr, layout)
                self._draw_shimmer(cr, w, h)
            elif self.mode == "transient" and self._transient is not None:
                t = self._transient
                color = {"typed": "ok", "copied": "accent",
                         "cancelled": "muted", "error": (1.0, 0.70, 0.73)}[t["kind"]]
                layout = text_layout(cr, t["text"])
                set_color(cr, color)
                cr.move_to(20, (h - layout.get_pixel_size()[1]) / 2)
                PangoCairo.show_layout(cr, layout)
                if t["retry"]:
                    self._draw_retry(cr, w - 78, h)
                self._regions["dismiss"] = (0, 0, w, h)
            return True

        def _draw_ring(self, cr, cx, cy) -> None:
            total = max(cfg.recording.silence_sec, 0.1)
            elapsed = time.monotonic() - self.countdown_at
            frac = max(0.0, min(1.0, (self.countdown - elapsed) / total))
            set_color(cr, (1, 1, 1), 0.15)
            cr.set_line_width(2.6)
            cr.arc(cx, cy, 8, 0, 2 * math.pi)
            cr.stroke()
            set_color(cr, "rec")
            cr.arc(cx, cy, 8, -math.pi / 2, -math.pi / 2 + 2 * math.pi * frac)
            cr.stroke()

        def _draw_wave(self, cr, x, width, h, frozen=False) -> None:
            bars = WAVE_BARS
            gap = width / bars
            mid = h / 2
            recent = self.levels[-bars:]
            set_color(cr, "ink", 0.35 if frozen else 0.9)
            cr.set_line_width(3)
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            for i in range(bars):
                if i < len(recent):
                    rms, threshold = recent[i - len(recent)]
                    norm = min(1.0, rms / max(threshold * 2.0, 1.0))
                else:
                    norm = 0.0
                bar = 2.0 + norm * (h * 0.62 - 2.0)
                bx = x + gap * i + gap / 2
                cr.move_to(bx, mid - bar / 2)
                cr.line_to(bx, mid + bar / 2)
                cr.stroke()

        def _draw_shimmer(self, cr, w, h) -> None:
            phase = (time.monotonic() % 1.4) / 1.4
            cx = (phase * 1.6 - 0.3) * w
            grad = cairo.LinearGradient(cx - 40, 0, cx + 40, 0)
            grad.add_color_stop_rgba(0, 0.42, 0.64, 1.0, 0)
            grad.add_color_stop_rgba(0.5, 0.42, 0.64, 1.0, 0.16)
            grad.add_color_stop_rgba(1, 0.42, 0.64, 1.0, 0)
            rounded_rect(cr, 0, 0, w, h, h / 2)
            cr.set_source(grad)
            cr.fill()

        def _draw_cancel(self, cr, cx, cy) -> None:
            set_color(cr, "muted")
            cr.set_line_width(1.8)
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            r = 4.2
            cr.move_to(cx - r, cy - r)
            cr.line_to(cx + r, cy + r)
            cr.move_to(cx + r, cy - r)
            cr.line_to(cx - r, cy + r)
            cr.stroke()
            self._regions["cancel"] = (int(cx - 14), 0, 28, PILL_H)

        def _draw_retry(self, cr, x, h) -> None:
            layout = text_layout(cr, "↻ retry")
            tw, th = layout.get_pixel_size()
            bw, bh = tw + 18, 22
            by = (h - bh) / 2
            rounded_rect(cr, x, by, bw, bh, bh / 2)
            set_color(cr, "accent", 0.5)
            cr.set_line_width(1.2)
            cr.stroke()
            set_color(cr, "accent")
            cr.move_to(x + 9, (h - th) / 2)
            PangoCairo.show_layout(cr, layout)
            self._regions["retry"] = (int(x) - 4, 0, int(bw) + 8, PILL_H)

        # -- input ------------------------------------------------------

        def _click(self, _w, event) -> bool:
            for name, (x, y, rw, rh) in self._regions.items():
                if name == "dismiss":
                    continue  # only if nothing else matched
                if x <= event.x <= x + rw and y <= event.y <= y + rh:
                    self._act(name)
                    return True
            if "dismiss" in self._regions:
                self._act("dismiss")
                return True
            return True  # the center is dead during recording — by design

        def _crossing(self, _w, _event, inside: bool) -> bool:
            self.hovered = inside
            self.on_hover(inside)
            return False

        def _act(self, name: str) -> None:
            if name == "cancel":
                run_vani("cancel")
            elif name == "retry":
                self.dismiss()
                run_vani("retry")
            elif name == "dismiss":
                self.dismiss()

    # ------------------------------------------------------------------
    # The caption card

    class CaptionCard:
        """The live draft, growing upward (or downward) out of the pill."""

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
            w.set_name("vani-card")
            visual = w.get_screen().get_rgba_visual()
            if visual is not None:
                w.set_visual(visual)
            w.set_app_paintable(True)
            css = Gtk.CssProvider()
            css.load_from_data(f"""
                #vani-card {{
                    background-color: rgba(14, 16, 22, {ui.opacity});
                    border-radius: 14px;
                }}
                #vani-card-text {{ color: #f2f2f2; font-size: 15px; }}
            """.encode())
            Gtk.StyleContext.add_provider_for_screen(
                w.get_screen(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            for setter in (box.set_margin_top, box.set_margin_bottom,
                           box.set_margin_start, box.set_margin_end):
                setter(12)
            self.text = Gtk.Label(xalign=0)
            self.text.set_name("vani-card-text")
            self.text.set_line_wrap(True)
            self.text.set_xalign(0)
            self.text.set_yalign(0)
            self.text.set_size_request(ui.width - 24, -1)
            self.scroll = Gtk.ScrolledWindow()
            self.scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            self.scroll.set_max_content_height(ui.max_height)
            self.scroll.add(self.text)
            box.pack_start(self.scroll, True, True, 0)
            w.add(box)
            w.set_default_size(ui.width, -1)
            w.connect("size-allocate", lambda *_: self._position())
            self.visible = False
            self._shown = None

        def update(self, text: str, hovering: bool = False) -> None:
            mode = cfg.ui.captions
            if not text or mode == "off" or (mode == "hover" and not hovering):
                self.hide()
                return
            if text != self._shown:
                if self._shown is None:
                    self.win.resize(cfg.ui.width, 1)
                self._shown = text
                self.text.set_text(text)
                height = self.text.get_preferred_height_for_width(
                    cfg.ui.width - 24)[1]
                self.scroll.set_min_content_height(
                    min(height, cfg.ui.max_height))
                GLib.idle_add(self._scroll_to_end)
            if not self.visible:
                self.visible = True
                self.win.show_all()
                self._position()

        def hide(self) -> None:
            if self.visible:
                self.visible = False
                self._shown = None
                self.win.hide()
                self.win.resize(cfg.ui.width, 1)

        def _scroll_to_end(self) -> bool:
            adj = self.scroll.get_vadjustment()
            adj.set_value(adj.get_upper() - adj.get_page_size())
            return False

        def _position(self) -> None:
            geo = active_monitor()
            if geo is None:
                return
            w, h = self.win.get_size()
            pos = cfg.ui.position
            margin_h = 24
            if pos.endswith("left"):
                x = geo.x + margin_h
            elif pos.endswith("right"):
                x = geo.x + geo.width - w - margin_h
            else:
                x = geo.x + (geo.width - w) // 2
            # Stacked beyond the pill: above it at the bottom, below at the top.
            if pos.startswith("top"):
                y = geo.y + 72 + PILL_H + 10
            else:
                y = geo.y + geo.height - 72 - PILL_H - 10 - h
            self.win.move(x, y)

    # ------------------------------------------------------------------
    # Tray icons, generated at runtime — one glyph, five colors.

    def build_icons() -> str:
        directory = paths.runtime_dir() / "icons"
        directory.mkdir(parents=True, exist_ok=True)
        for name, rgb in ICON_STATES.items():
            surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 22, 22)
            cr = cairo.Context(surface)
            cr.set_source_rgba(*rgb, 1.0)
            cr.set_line_width(1.8)
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            # Mic capsule
            rounded_rect(cr, 8.2, 3.0, 5.6, 9.5, 2.8)
            cr.fill()
            # Cradle arc, stem, base
            cr.arc(11, 10.5, 5.2, 0.15, math.pi - 0.15)
            cr.stroke()
            cr.move_to(11, 15.7)
            cr.line_to(11, 18.2)
            cr.stroke()
            cr.move_to(7.8, 18.2)
            cr.line_to(14.2, 18.2)
            cr.stroke()
            if name == "off":
                cr.set_source_rgba(*rgb, 1.0)
                cr.set_line_width(2.2)
                cr.move_to(4, 19)
                cr.line_to(18, 3)
                cr.stroke()
            surface.write_to_png(str(directory / f"vani-{name}.png"))
        return str(directory)

    def run_vani(*args: str) -> None:
        subprocess.Popen([sys.argv[0], *args]
                         if sys.argv[0].endswith("vani")
                         else [sys.executable, "-m", "vani", *args])

    # ------------------------------------------------------------------
    # The tray + the event plumbing

    class Tray:
        def __init__(self) -> None:
            self.ind = AppIndicator3.Indicator.new(
                "vani", "vani-asleep",
                AppIndicator3.IndicatorCategory.APPLICATION_STATUS)
            self.ind.set_icon_theme_path(build_icons())
            self.ind.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
            self.ind.set_title("vani")
            self.menu = Gtk.Menu()
            self.ind.set_menu(self.menu)

            self.pill = Pill() if cfg.ui.enabled else None
            self.card = CaptionCard() if cfg.ui.enabled else None
            if self.pill is not None:
                self.pill.on_hover = self._on_hover
            self.coarse = ""       # the menu/icon granularity
            self.server = ("unpolled", "")
            self.connected = False
            self._live_text = ""
            self._card_linger_seq = 0

            self.client = EventClient(
                on_event=lambda ev: GLib.idle_add(self._on_event, ev),
                on_connect=lambda: GLib.idle_add(self._set_connected, True),
                on_disconnect=lambda: GLib.idle_add(self._set_connected, False))
            self.client.start()
            GLib.timeout_add(500, self._poll_fallback)
            self._render_coarse(state.IDLE)

        # -- event handling (GTK thread) --------------------------------

        def _set_connected(self, yes: bool) -> bool:
            self.connected = yes
            return False

        def _on_event(self, ev: dict) -> bool:
            kind = ev.get("ev")
            if kind == "state":
                self._render_coarse(ev.get("state", state.IDLE),
                                    ev.get("countdown", 0.0))
            elif kind == "level" and self.pill is not None:
                self.pill.add_level(float(ev.get("rms", 0)),
                                    float(ev.get("threshold", 1)))
            elif kind == "live":
                self._live_text = ev.get("text", "")
                if self.coarse in ("rec", "busy"):
                    self._refresh_card()
            elif kind == "result":
                self._on_result(ev)
            elif kind == "error":
                message = ev.get("message", "something went wrong")
                seconds = BLOCKED_SEC if ev.get("blocked") else ERROR_SEC
                if self.pill is not None:
                    self.pill.show_transient("error", "✗ " + message, seconds,
                                             retry=bool(ev.get("retry")))
                # Words the stream had already returned are kept, not wiped:
                # the card holds them so they can be read or copied.
                self._show_partial(ev.get("partial", ""))
            elif kind == "discarded":
                cancelled = ev.get("reason") == "cancelled"
                partial = ev.get("partial", "")
                if self.pill is not None and cancelled:
                    self.pill.show_transient(
                        "cancelled",
                        "✕ cancelled — audio kept" + (" and text saved"
                                                      if partial else ""),
                        CANCELLED_SEC)
                self._show_partial(partial)
            elif kind == "server":
                ok = ev.get("ok")
                self.server = (ok, ev.get("detail", ""))
                if self.pill is not None:
                    self.pill.server_ok = ok
                    if self.pill.mode == "dot":
                        self.pill.win.queue_draw()
                self._rebuild()
            elif kind == "slow":
                if self.pill is not None and self.pill.mode == "finishing":
                    self.pill.slow = True
                    self.pill._apply()
            elif kind == "mode":
                if self.pill is not None:
                    self.pill.ptt = bool(ev.get("ptt"))
            return False

        def _on_result(self, ev: dict) -> None:
            backend = ev.get("backend", "")
            target = ev.get("target", "")
            if self.pill is not None:
                if backend == "clipboard":
                    label = "📋 copied to clipboard"
                    kind = "copied"
                else:
                    verb = "sent" if ev.get("submitted") else "typed"
                    label = f"✓ {verb}" + (f" → {target}" if target else "")
                    kind = "typed"
                self.pill.show_transient(kind, label, TYPED_SEC)
            if self.card is not None:
                # Linger on the final transcript so it can be read, then go.
                # Worth a look even in hover mode: this is the delivered text.
                self.card.update(ev.get("text", "") or self._live_text,
                                 hovering=True)
                self._card_linger_seq += 1
                seq = self._card_linger_seq
                GLib.timeout_add(int(LINGER_SEC * 1000),
                                 self._card_linger_end, seq)

        def _show_partial(self, partial: str) -> None:
            """Hold a salvaged draft on the card, then let it go."""
            if self.card is None:
                return
            if not partial:
                self.card.hide()
                return
            self.card.update(partial, hovering=True)
            self._card_linger_seq += 1
            seq = self._card_linger_seq
            GLib.timeout_add(int(ERROR_SEC * 1000), self._card_linger_end, seq)

        def _refresh_card(self, text: "str | None" = None) -> None:
            """Show the draft according to `ui.captions` and the pointer."""
            if self.card is None:
                return
            hovering = self.pill is not None and self.pill.hovered
            self.card.update(self._live_text if text is None else text,
                             hovering=hovering)

        def _on_hover(self, inside: bool) -> None:
            """Pointer crossed the pill — `hover` captions live and die here."""
            if cfg.ui.captions != "hover":
                return
            if inside and self._live_text and self.coarse in ("rec", "busy"):
                self._refresh_card()
            elif not inside:
                self.card.hide()

        def _card_linger_end(self, seq: int) -> bool:
            if seq == self._card_linger_seq and self.coarse not in ("rec",):
                if self.card is not None:
                    self.card.hide()
            return False

        def _render_coarse(self, st: str, countdown: float = 0.0) -> None:
            mapping = {
                state.IDLE: "asleep",
                state.RECORDING: "rec",
                state.SILENCE: "rec",
                state.TRANSCRIBING: "busy",
                state.DISABLED: "off",
            }
            coarse = mapping.get(st, "asleep")
            if coarse == "asleep" and self.server[0] is False:
                coarse = "blocked"
            if st == state.RECORDING and self.coarse != "rec":
                # A session is starting: this is the one moment worth asking
                # which screen the user is working on, before the pill grows.
                active_monitor(refresh=True)
            if self.pill is not None:
                if st == state.RECORDING:
                    self.pill.show_mode("listening")
                elif st == state.SILENCE:
                    self.pill.set_countdown(countdown)
                    self.pill.show_mode("countdown")
                elif st == state.TRANSCRIBING:
                    self.pill.slow = False
                    self.pill.show_mode("finishing")
                elif st == state.DISABLED:
                    self.pill.show_mode("hidden")
                else:
                    self.pill.show_mode("dot")
            if self.card is not None:
                if st in (state.RECORDING, state.SILENCE):
                    self._refresh_card()
                elif st == state.DISABLED:
                    self.card.hide()
            if st == state.IDLE:
                self._live_text = ""
            if coarse != self.coarse:
                self.coarse = coarse
                self.ind.set_icon_full(f"vani-{coarse}", coarse)
                self._rebuild()

        # -- fallback polling (daemon without a bus, or none at all) -----

        def _poll_fallback(self) -> bool:
            if self.connected:
                return True
            current, countdown = state.read_status()
            server = state.read_server()
            if server != self.server:
                self.server = server
                if self.pill is not None:
                    self.pill.server_ok = server[0]
            self._live_text = state.read_live()
            self._render_coarse(current, countdown)
            return True

        # -- menu --------------------------------------------------------

        def _rebuild(self) -> None:
            for child in self.menu.get_children():
                self.menu.remove(child)

            self._append_label(self._headline())
            if self.server[0] is False:
                self._append_label("Server DOWN — " + (self.server[1] or "?"))

            if self.coarse != "off":
                toggle = Gtk.MenuItem(
                    label="Stop & type" if self.coarse == "rec"
                    else "Start dictation")
                toggle.connect("activate", lambda *_: run_vani("toggle"))
                self.menu.append(toggle)
            if self.coarse == "rec":
                cancel = Gtk.MenuItem(label="Cancel (discard)")
                cancel.connect("activate", lambda *_: run_vani("cancel"))
                self.menu.append(cancel)

            onoff = Gtk.MenuItem(
                label="Enable dictation" if self.coarse == "off"
                else "Disable dictation (close mic)")
            onoff.connect("activate", lambda *_: run_vani(
                "enable" if self.coarse == "off" else "disable"))
            self.menu.append(onoff)

            self.menu.append(Gtk.SeparatorMenuItem())
            recent = Gtk.MenuItem(label="Recent transcripts")
            recent.set_submenu(self._history_menu())
            self.menu.append(recent)

            settings = Gtk.MenuItem(label="Settings")
            settings.set_submenu(self._settings_menu())
            self.menu.append(settings)

            quit_item = Gtk.MenuItem(label="Quit vani")
            quit_item.connect("activate", self.quit_everything)
            self.menu.append(quit_item)
            self.menu.show_all()

        def _headline(self) -> str:
            wake = ""
            try:
                if cfg.wake.enabled and cfg.wake.phrases:
                    wake = f" — say “{cfg.wake.phrases[0]}” or press the key"
            except Exception:
                pass
            return {
                "asleep": "Asleep" + wake,
                "blocked": "Blocked — server unreachable",
                "rec": "● Listening…",
                "busy": "Finishing…",
                "off": "Off — microphone closed",
            }.get(self.coarse, "vani")

        def _history_menu(self) -> "Gtk.Menu":
            sub = Gtk.Menu()
            entries = state.read_history(RECENT_ITEMS)
            if not entries:
                item = Gtk.MenuItem(label="(no transcripts yet)")
                item.set_sensitive(False)
                sub.append(item)
            for _stamp, text in entries:
                label = (text if len(text) <= MAX_ITEM_CHARS
                         else text[: MAX_ITEM_CHARS - 3] + "...")
                item = Gtk.MenuItem(label=label)
                item.connect("activate", self.copy, text)
                sub.append(item)
            if entries:
                full = Gtk.MenuItem(label="Open full history")
                full.connect("activate", lambda *_: subprocess.Popen(
                    ["xdg-open", str(paths.history_file())]))
                sub.append(full)
            retry = Gtk.MenuItem(label="Retry last recording")
            retry.connect("activate", lambda *_: run_vani("retry"))
            sub.append(retry)
            return sub

        def _settings_menu(self) -> "Gtk.Menu":
            sub = Gtk.Menu()

            mic = Gtk.MenuItem(label="Microphone")
            mic.set_submenu(self._mic_menu())
            sub.append(mic)

            pos = Gtk.MenuItem(label="Pill position")
            pos.set_submenu(self._position_menu())
            sub.append(pos)

            captions = Gtk.MenuItem(label="Live captions")
            captions.set_submenu(self._captions_menu())
            sub.append(captions)

            submit = Gtk.CheckMenuItem(label="Press Enter after typing")
            submit.set_active(cfg.output.submit)
            submit.connect("toggled", self._toggle_submit)
            sub.append(submit)

            sound = Gtk.CheckMenuItem(label="Sounds")
            sound.set_active(cfg.ui.sounds)
            sound.connect("toggled", self._toggle_sounds)
            sub.append(sound)

            dot = Gtk.CheckMenuItem(label="Idle dot")
            dot.set_active(cfg.ui.idle_dot)
            dot.connect("toggled", self._toggle_idle_dot)
            sub.append(dot)

            login = Gtk.CheckMenuItem(label="Start on login")
            login.set_active(service.starts_on_login())
            # Connected after set_active, so building the menu never toggles.
            login.connect("toggled", lambda item: threading.Thread(
                target=service.set_start_on_login, args=(item.get_active(),),
                daemon=True).start())
            sub.append(login)

            sub.append(Gtk.SeparatorMenuItem())
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

        def _position_menu(self) -> "Gtk.Menu":
            sub = Gtk.Menu()
            for pos in UI_POSITIONS:
                item = Gtk.CheckMenuItem(label=pos.replace("-", " "))
                item.set_draw_as_radio(True)
                item.set_active(cfg.ui.position == pos)
                item.connect("activate", self._set_position, pos)
                sub.append(item)
            return sub

        def _set_position(self, _item, pos: str) -> None:
            if cfg.ui.position == pos:
                return
            cfg.ui.position = pos
            self._save("position", pos)
            if self.pill is not None and self.pill.visible:
                self.pill._apply()
            self._rebuild()

        def _captions_menu(self) -> "Gtk.Menu":
            sub = Gtk.Menu()
            labels = {"always": "Always", "hover": "On hover", "off": "Off"}
            for mode in UI_CAPTIONS:
                item = Gtk.CheckMenuItem(label=labels[mode])
                item.set_draw_as_radio(True)
                item.set_active(cfg.ui.captions == mode)
                item.connect("activate", self._set_captions, mode)
                sub.append(item)
            return sub

        def _set_captions(self, _item, mode: str) -> None:
            if cfg.ui.captions == mode:
                return
            cfg.ui.captions = mode
            self._save("captions", mode)
            if self.card is not None:
                if mode == "off" or (mode == "hover"
                                     and not (self.pill and self.pill.hovered)):
                    self.card.hide()
                elif self.coarse in ("rec", "busy"):
                    self._refresh_card()
            self._rebuild()

        def _toggle_submit(self, item) -> None:
            cfg.output.submit = item.get_active()
            try:
                config_mod.set_key("output", "submit", cfg.output.submit)
            except OSError:
                pass
            daemon_mod.signal_reload()  # the daemon owns the typist

        def _toggle_sounds(self, item) -> None:
            cfg.ui.sounds = item.get_active()
            self._save("sounds", cfg.ui.sounds)
            daemon_mod.signal_reload()  # the daemon plays them; tell it now

        def _toggle_idle_dot(self, item) -> None:
            cfg.ui.idle_dot = item.get_active()
            self._save("idle_dot", cfg.ui.idle_dot)
            if self.pill is not None and self.pill.mode == "dot":
                self.pill._apply()

        def _save(self, key: str, value) -> None:
            try:
                config_mod.set_key("ui", key, value)
            except OSError:
                pass

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
                             lambda *_: run_vani("mic", "set", value))
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

        def _append_label(self, text: str) -> None:
            item = Gtk.MenuItem(label=text)
            item.set_sensitive(False)
            self.menu.append(item)

        # -- actions -----------------------------------------------------

        def check_server(self) -> None:
            """Off the GTK thread: probe health and publish the verdict."""
            from .client import ServerError, check_health
            from .config import ConfigError

            try:
                check_health(load_config(required=False))
                state.set_server(True)
            except (ServerError, ConfigError) as exc:
                state.set_server(False, str(exc))
            # the poll fallback (or the daemon's own probe) picks it up.

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
