# vani UX redesign — audit, research, options, recommendation

*Part 1–2 (inventory + audit) are grounded in the current code. Part 3 is a
digest of research on best-in-class dictation and voice-agent apps (Wispr
Flow, superwhisper, Aqua Voice, MacWhisper, Apple/Windows dictation, Talon,
Linux tools) plus the HCI literature on peripheral interfaces. Parts 4–6 are
the redesign: principles, four design directions, and a recommendation with a
phased roadmap. A visual mockup of the recommended design lives next to this
file: `docs/ux-mockup.html`.*

---

## 1. What the UX is today — full inventory

vani currently talks to the user through **six different surfaces**, split
across two processes plus a fallback code path:

| # | Surface | Owned by | What it shows |
|---|---------|----------|---------------|
| 1 | Tray icon | `vani tray` | 5 borrowed GNOME symbolic icons for idle / recording / silence / transcribing / disabled |
| 2 | Tray menu | `vani tray` | status line, server verdict, start/stop/cancel/disable, last 5 transcripts (click = copy), history, settings submenu, quit |
| 3 | Caption overlay | `vani tray` | translucent 520 px box, bottom-center of primary monitor; header = state + countdown text; body = live transcript; lingers 2 s on success, **vanishes silently on failure/cancel** |
| 4 | Notification slot 1 | daemon | live-text fallback when tray is dead, "✓ typed" confirmation (always), errors, "no speech detected", cancelled, disabled/enabled |
| 5 | Notification slot 2 | daemon | server unreachable / back online banners |
| 6 | CLI | `vani status/doctor/history/mic` | diagnostics and history |

Plus a seventh, hidden variant: when the daemon is *not* running, `vani
toggle` runs a **standalone path** (`toggle.py`) with its own notification
strings and no overlay, no live text.

And one channel that is entirely unused: **sound**. There are no earcons at
all — no start cue, no stop cue, no error cue.

### What is already genuinely good

Worth saying before the critique, because the redesign should not lose these:

- **Auto-stop on silence with a visible countdown, and speak-to-cancel.** This
  is the correct core interaction and better than many commercial apps.
- **Nothing typed until the end** — no un-typing, no garbage in the field.
- **Live streaming captions** a word or two behind the voice.
- **Health monitoring that dedups** — one banner when the server dies, one when
  it's back, never one per failed recording.
- **Disabled ≠ ignoring**: mic actually closed. A real privacy stance.
- **`vani doctor`** — the CLI diagnostics story is excellent.
- The state machine (`session.py`) is clean and desktop-agnostic — the right
  foundation to hang a better UI on.

The problem is not the machinery. It's that the *presentation layer* grew one
surface at a time, each solving yesterday's complaint, and no single surface
owns the experience.

---

## 2. The audit — why it feels incoherent

### 2.1 Three surfaces share one job, chosen by hidden state

"What is vani doing right now?" is answered by the overlay, *or* a
notification, *or* the tray icon — and which one you get depends on state the
user can't see: is the tray process alive (`_ui_live` checks a pidfile), is
`ui.enabled` set, did the daemon or the standalone toggle path handle this
recording. The same event — "recording started" — can manifest as an overlay
header, a notification banner, or nothing. A user cannot build a mental model
of a UI that moves around.

### 2.2 Success is double-reported; failure is under-reported

- On success you get the overlay lingering with "✓ typed" **and** a "✓ *text*"
  notification (`deliver()` fires it unconditionally, even when the overlay is
  up). Two confirmations for one event — and every transcript lands
  permanently in GNOME's notification history.
- On failure or cancel, the surface you were actually watching — the overlay —
  simply **disappears** (`tick_overlay`: "failed or cancelled: nothing to
  show"), and the explanation appears in a *different corner of the screen* as
  a notification. The emotional read is "it crashed", and the eye has to hunt.

The rule should be the opposite: success can be quiet, failure must be loud
*in the place you were looking*.

### 2.3 There is no "is it hearing me?" signal

The single biggest failure class in vani's own troubleshooting section —
wrong mic, quiet mic, Bluetooth profile — has **zero live feedback**. No
level meter, no waveform, nothing moves with your voice. The user only learns
after the fact, via "(no speech detected — mic: …)". Every serious voice
product shows input energy *while you speak*, because it collapses "is it
broken?" into a glance.

### 2.4 The audio channel is unused

Dictation is an *eyes-busy* activity: the user is looking at their document,
their code, their browser field — not at the bottom-center of the primary
monitor. The wake-word flow is explicitly hands-free and often eyes-free.
Yet every confirmation vani gives is visual. Say "hey claude" while looking
at a second monitor and you get no evidence it woke up until words appear in
a place you aren't looking. Earcons (short start/stop/error sounds) are the
standard solution and cost almost nothing.

### 2.5 States exist that the UI never shows

- **Server down, while idle**: visible only as a text line *inside* the tray
  menu — you must click to discover dictation is dead. The moment of truth
  (you press the key) starts a recording that is doomed to fail 20 s later.
- **WebSocket connecting / not yet connected** at recording start.
- **Wake word heard** — the transition moment has no dedicated cue.
- **Typed vs copied-to-clipboard** delivery — different user action required,
  distinguished only by notification wording.
- **Disabled discoverability**: disable it today, say "hey claude" tomorrow —
  absolute silence. Nothing says "the mic is closed".

### 2.6 The tray is a grab-bag; the icons are borrowed

The menu mixes live status lines, actions, five transcripts-as-menu-items
(click to copy — surprising), a settings submenu whose main entry opens a
TOML file, and daemon ops (restart, check server). The five state icons are
generic GNOME symbols — "silence" is a *calendar* icon
(`appointment-soon-symbolic`), transcribing is a sync emblem. None of it is
learnable brand language, and on stock GNOME the whole tray needs an
extension and renders 16 px tiny.

### 2.7 The overlay is a caption box, not an instrument

Fixed 520 px dark rectangle. The header line multiplexes state + countdown as
*text* ("⏸ typing in 2 s — speak to continue") — which requires reading, and
peripheral vision can't read. A countdown should be *shape* (a draining ring
or bar), speech energy should be *motion* (a waveform). Text is the one thing
the box does show — but always full-width even for a two-word utterance, and
always at bottom-center of the primary monitor regardless of where you're
typing.

### 2.8 Cancel is buried; there is no hold-to-talk

"Oops, discard that" is a top-3 action in real dictation. Today it requires
opening the tray menu or having pre-bound a second shortcut to `vani cancel`.
No Esc, no double-press gesture. And the only key mode is toggle — there is
no press-and-hold → release-to-send, which is both the fastest ergonomics for
short utterances and eliminates the 3-second silence wait entirely.

### 2.9 The architecture caps the UX

Tray and overlay learn everything by **polling files** (500 ms / 120 ms).
That works for coarse state, but it cannot carry a 30 fps mic level for a
waveform, or transient events (wake heard, error details), and it forces the
"two processes agree via pidfiles" fallback dance of §2.1. Any richer UI
needs the daemon to *push* events (a Unix socket) to a single UI process that
owns all presentation.

### 2.10 Notifications are being used as an app UI

Replacing a notification in-place every 0.3 s over `gdbus` is fighting the
notification system's nature. Notifications are for *when you're not there*;
GNOME queues them, animates them, logs them to history, and silences them in
Do Not Disturb — which silently kills vani's entire live UX. Everything
about an *active, attended* session belongs on an app-owned surface.

---

## 3. What the best apps do — research digest

Deep dives on Wispr Flow, superwhisper, Aqua Voice, MacWhisper, Apple
macOS/iOS dictation, Windows Voice Typing and Voice Access, Talon, ChatGPT
voice mode, and the Linux field (whisper-overlay, Handy, nerd-dictation,
Speech Note). What they converge on:

**The surface.** Every serious third-party app landed on **a small pill at a
screen edge** — and then every one of them was forced by user backlash to make
it *movable and hideable* (Wispr's bottom-center bar covering Gmail's send
button drew a 700-response revolt before drag-docking shipped). MacWhisper's
placement model is the most complete: center-top / center-bottom / at-caret /
hidden, user's choice. Apple alone anchors state to the caret itself (the
pulsing/glowing insertion point) — minimal eye travel, but it needs text-system
integration nobody else has. Windows Voice Access uses the opposite extreme, a
full-width docked bar — maximally legible, permanently occupying space.

**Activation.** The de facto standard is now **one key, two semantics: hold =
push-to-talk, tap (or double-tap) = hands-free toggle** (Wispr `fn`,
Aqua `Fn`, superwhisper's single shortcut, MacWhisper right-⌘). Hold-to-talk
is a *quasimode* in Raskin's sense — a mode maintained by muscle, like Shift —
so it cannot be forgotten; latched toggle modes are where hot-mic accidents
live. Release-to-send also eliminates the stop-detection problem entirely: the
finger already knows the state, so there is no silence wait and no countdown.

**"Is it hearing me."** Universally answered with an **animated waveform /
level meter** during recording. superwhisper's docs even use a *static*
waveform as the diagnostic for mic problems. The waveform isn't decoration —
it is the wrong-mic detector, read from peripheral vision as pure motion.

**State language.** The best apps use a tiny **color/animation state machine
mirrored in both the pill and the tray**: superwhisper's dot is yellow
(loading) → red (recording) → blue (processing) → green (done);
whisper-overlay's Waybar module is gray/blue/red. Either surface alone
suffices; both always agree.

**Sound.** Two earcons — **a start cue you wait for** ("Speak after the
ping") **and a completion cue you can feel** — rising = start, falling = done
is the OS-wide convention. Always disableable (Apple's non-disableable beep
is a top complaint), volume decoupled from system volume, optional
music-ducking while recording. Sound is the designated fallback channel when
the overlay is hidden — superwhisper officially supports "overlay off, audio
cues on."

**Live text.** Two defensible schools. *Commit-at-once* (Wispr): streaming raw
ASR is "overstimulating and jarring… Flow will wait, understand, and then
write what you meant"; nothing shows until the polished text pastes.
*Stream-it* (Aqua Realtime, Apple, Windows): words appear immediately and get
revised in place — which produces the retroactive-correction jank complaints
("watching the iPhone type words and then change them right before
sending"). The cleanest compromise, used by whisper-overlay on Linux: **draft
live in your own overlay, commit the final text once** — exactly the
architecture vani already has. Notably, nobody styles provisional words *as*
provisional (grey/italic); that's an open opportunity.

**Stopping.** The pro apps **do not auto-stop on silence** — stop is always
explicit (release or tap), silence is *removed from the audio* instead. The
OS products that do auto-stop (macOS 30 s, Win+H 5–10 s) collect "it stopped
mid-thought" complaints; iOS 18's more aggressive pause detection made this
worse. For always-listening designs, Windows Voice Access has the most
legible model found anywhere: it never dies, it **sleeps** — three named mic
states (Listening / Sleep / Off), wake phrases, state always displayed.

**Errors.** Wispr's error taxonomy is the gold standard, especially relevant
to a remote-ASR client: offline → **the bar grays out before you speak**;
connection lost mid-dictation → **audio auto-saved and re-transcribed on
reconnect** ("Your dictation was interrupted — tap to view"); "Is your
microphone muted?" / "Audio is silent" / "No microphone detected" as distinct
messages; "Taking longer than usual" past a threshold. And **history as a
safety net is universal**: Esc cancels but the audio is kept; nothing the
user ever said is lost to an error.

**Latency.** Wispr publishes a **700 ms end-of-speech → text budget** ("any
slower and users get impatient"); Aqua ~450 ms. HCI numbers agree: human
turn-taking gaps are ~200 ms; delay is felt unconsciously past ~300 ms,
noticed past ~500 ms, and breaks flow of thought past ~1 s — beyond which an
explicit processing state (shimmer, spinner, pill state) is mandatory.

**Anti-patterns catalog** (each one shipped and regretted somewhere):
overlay occluding the target app; overlay stealing focus and breaking the
paste (Handy's README admits it; superwhisper fixed it twice); silent
failure (dictation that "works" but types nowhere — the ydotool trap vani
already documents); losing speech on error; clipboard pollution; mandatory
sounds; mode state you can't see (Talon's community built an entire HUD
ecosystem to fix this); pressing Enter on the user's behalf.

**The Linux gap.** None of the premium-UX apps run on Linux (Wispr waitlist,
Aqua requested since 2024, superwhisper/MacWhisper Mac-only, Talon X11-only).
The only native live overlay is whisper-overlay, wlroots-only — it doesn't
run on GNOME. **The polished-dictation-UX field on Linux is genuinely
empty.** vani is closer to filling it than anything shipping.

*(Full per-product findings with sources are in the research appendix at the
bottom of this doc.)*

---

## 4. Principles for vani

Derived from the audit × the research. These are the tests any design option
must pass.

1. **One surface owns the session.** Everything about an *attended, active*
   recording — state, level, countdown, live text, errors — appears in exactly
   one place, always the same place. Notifications are reserved for
   *unattended* events, and are a degraded fallback, never a peer surface.
2. **Motion and color for the periphery; words only at the focus.** Peripheral
   vision cannot read but is exquisitely tuned to motion and color change. So:
   waveform = hearing you, color = state, draining ring = countdown. Text
   (live captions, error strings) is for when the user chooses to look.
3. **Sound is a first-class channel.** Dictation is eyes-busy. Rising tick =
   mic hot ("speak after the ping"), falling tick = committed, distinct low
   buzz = error, subtle pop = wake-word acknowledged. All disableable, quiet
   by default.
4. **The mic's honesty is visible before you speak.** If the server is down,
   the mic is broken, or dictation is off, the user finds out *at or before
   the moment of intent* — a grayed/red idle state, a refusal with a reason —
   never 20 s after talking into a doomed recording.
5. **Failure is louder than success, in the same place.** Success: falling
   tick + brief ✓ + fade. Failure: the surface you were watching turns red,
   says why in one human sentence, and offers the recovery (retry / copy
   draft). It never simply disappears.
6. **Never lose speech.** Cancelled, failed, or unroutable transcripts (and
   the audio) are kept and recoverable — history is a safety net, not a log.
7. **Hold is the primary gesture.** Press-and-hold = quasimode PTT,
   release = send instantly (no silence wait — the biggest single latency win
   available). Tap = hands-free session with silence auto-stop (vani's
   existing flow, which the wake word also uses). One key, two semantics.
8. **Wake mode sleeps, it doesn't blur.** Adopt the Listening / Asleep / Off
   vocabulary. "Asleep" (wake word armed, audio analyzed locally only) is
   visually distinct from "Off" (mic closed) — and both are visible at a
   glance, not buried in a menu.
9. **Latency over ~500 ms gets a face.** Commit target is sub-second;
   the finishing state is visible (shimmer), and past ~4 s it says "taking
   longer than usual" rather than looking hung.
10. **Chrome is invisible; evidence is not.** The goal is Wispr's "disappear
    into muscle memory" — minimal idle footprint — but what the system *heard
    and did* stays inspectable (live draft, per-utterance history, echo of
    the last action). Users retrofit this onto every system that hides it.

---

## 5. Four design directions

All four assume the same plumbing fix (§6.1: daemon pushes events over a
socket; one UI process owns presentation; notifications demoted to fallback).
They differ in what the *surface* is.

### Option A — "The Instrument": one adaptive pill

*The Wispr/superwhisper lineage, tuned for vani's streaming architecture.*

A single morphing pill, default bottom-center but dockable to any edge/corner
and remembered. It is the only session surface; every state is a shape:

```
 Off        ∅ nothing shown (tray icon shows muted mic)
 Asleep     ‥ a 6px dim dot — "wake word armed" (hover: "say 'hey claude'")
 Listening  ▁▂▅▂▇▂  live waveform in a pill + red dot; grows from the dot
            with a spring animation the instant the wake word / key fires
 Countdown  the red dot becomes a draining ring (3s → 0); waveform stays
 Finishing  waveform freezes, shimmer sweeps the pill (≤1s typically)
 Typed      pill flashes ✓ green, falling tick, fades out (400ms)
 Error      pill turns red, one sentence ("Server unreachable — audio
            saved, press ↻ to retry"), stays 8s or until dismissed
 Blocked    idle dot is amber when the server is down — you know before
            you speak; pressing the key gives the error state immediately
```

Live captions are a *card that grows upward out of the pill* — display modes
`always` / `hover` / `off` (config + tray). The card is the current overlay,
re-skinned and attached to the instrument instead of floating separately.

- Interactions: key press/hold per principle 7; **double-press = cancel**
  (with its own descending earcon); pill's stop/cancel buttons clickable —
  except the center, which is dead during recording (Wispr's anti-misclick
  detail).
- Tray becomes small: colored dot mirroring pill state, toggle, enable/
  disable, history, settings. No status prose, no transcripts-as-menu-items.
- **Pros**: minimal footprint; matches the muscle-memory goal; evolutionary —
  reuses the existing GTK overlay process and most of the tray code.
- **Cons**: still an XWayland window on GNOME (focus-theft must be actively
  prevented); bottom-center default inherits Wispr's occlusion risk (hence
  docking); "agent-ness" is limited — it's an instrument, not a console.

### Option B — "The Console": a voice-agent strip

*The Voice Access lineage — for the "voice agent app" feeling.*

A slim horizontal strip (~32 px), top-center under the GNOME clock or
bottom-edge, auto-hide optional. Unlike the pill it is *persistent while
enabled* and carries named state — this is the option that treats vani as an
agent you converse with rather than a utility that flashes:

```
 ┌──────────────────────────────────────────────────────────────┐
 │ ● Listening   ▁▂▅▇▂▁   "…and that's why the demo failed"   ⏹ ✕ │
 └──────────────────────────────────────────────────────────────┘
   state chip    waveform   live echo (last words, one line)   actions
```

- Idle it collapses to a chip: `‥ Asleep — "hey claude"` / `⊘ Off` /
  `⚠ Server down`. The mic state is *always* on screen, Voice Access-style.
- Below the strip, a transient **event feed** (talon_hud lineage): the last
  2–3 utterances with their outcome — `✓ typed → Firefox`, `📋 copied
  (nothing focused)`, `✗ failed — retry` — each fading after a few seconds,
  clickable to copy/retry. The agent shows its work.
- This surface scales naturally into actual agent features later: command
  utterances ("hey claude, new paragraph"), routing ("type it in the
  terminal"), confirmations.
- **Pros**: maximal legibility; zero hidden state; the strongest "voice
  agent" identity; the event feed solves failure-visibility completely.
- **Cons**: permanent screen presence is the #1 backlash pattern in the
  research (mitigable with auto-hide, but that trades legibility back);
  bigger build; more visual weight than a dictation tool strictly needs.

### Option C — "Native Shell": a GNOME Shell extension

*The platform-native answer — out-of-the-box for a Linux app.*

You run GNOME 42 on Wayland. GNOME will never give an app window layer-shell
placement, but a **Shell extension** draws in the shell's own layer — the
same machinery as the volume OSD and the built-in screen-recording indicator:

- **Top-bar indicator**: a real mic glyph with the color state language
  (dim/red/blue/amber), always visible — no AppIndicator extension, no 16px
  borrowed icons. Recording adds a red recording-dot next to the clock,
  exactly where GNOME already puts screen-share/mic-privacy indicators —
  users already know to look there.
- **HUD**: a shell-layer pill/OSD for waveform + countdown + captions —
  *cannot* steal focus (it isn't a window), works over fullscreen apps,
  multi-monitor aware for free, follows the system theme.
- Daemon exposes a small **D-Bus interface** (`org.vani.Daemon`: state,
  level, live text signals; Toggle/Cancel/Enable methods) — which also gives
  you GNOME Quick Settings toggles, `busctl` scripting, and KDE/waybar
  frontends later.
- **Pros**: the only architecturally *correct* overlay on GNOME Wayland; the
  most native look; kills the whole XWayland/focus/positioning hack class;
  D-Bus API is valuable regardless.
- **Cons**: GNOME-only (the pill process remains as fallback for other
  desktops → two frontends to maintain); Shell-extension churn across GNOME
  versions; JS, not Python; review friction if ever distributed on
  extensions.gnome.org.

### Option D — "Ears First": zero chrome

*The invisible-interface extreme, as a mode rather than a product.*

No persistent visuals at all. The earcon vocabulary carries the session
(wake-ack pop → start tick → committed tick / error buzz), the GNOME
top-bar mic-privacy dot is the only recording indicator, and captions appear
only on demand (hold the key a beat longer, or hover the tray). History is
the inspection surface.

- **Pros**: absolute calm; nothing to occlude or steal focus; pairs
  perfectly with hold-to-talk (the finger knows the state, so the eyes
  don't need to).
- **Cons**: fails principles 4–5 for wake-word users (no pre-speech server
  verdict, no visible error surface); audio-only state is exclusionary
  (deaf/HoH users, muted speakers, loud rooms). **Verdict: not the product —
  but every piece of it should exist as the `ui.enabled = false` degraded
  mode, which today is "notifications only" and would become "earcons +
  tray + history".**

---

## 6. Recommendation

**Build Option A as the core, with B's legibility vocabulary, on C's
plumbing — and ship D's earcons first, this week.**

Concretely: the product is the **adaptive pill instrument** (A). It adopts
the **named mic states and per-utterance outcome echo** from the console (B)
— Asleep/Off/Blocked visible at a glance in the idle dot + tray, and the
"✓ typed / ✗ failed — retry" moment shown on the pill rather than in a
notification. The daemon grows the **event-push channel and D-Bus surface**
from (C), so the GTK pill is just the first frontend; a GNOME Shell
extension can replace/augment it later without touching the daemon. And the
**earcon set** (D) is orthogonal to everything and removes the worst
eyes-free gaps immediately.

Why not B as the product: the research is unambiguous that persistent bars
get rejected for occlusion; vani is a dictation tool first, and the agent
identity is better earned through behavior (outcome echo, future commands)
than through permanent pixels. Why not C first: it's the best end-state
overlay but couples the UX overhaul to a GNOME-extension build; the pill
gets you 90% of the experience desktop-agnostically, and nothing in it is
throwaway once the extension exists.

### 6.1 The structural fix that unlocks everything

Replace file-polling with **daemon → UI event push** over a Unix socket in
`$XDG_RUNTIME_DIR/vani/` (newline-JSON events: `state`, `level` (~20 Hz,
only while recording), `live_text`, `countdown`, `result`, `error`,
`server`). Keep writing the status file for `vani status` and as the crash
fallback. One UI process subscribes and owns *all* presentation; the daemon
never talks to the notification API except when no UI is connected — the
`_ui_live` pidfile dance, the double-reporting, and the standalone-toggle
divergence all collapse into "is a subscriber connected". (D-Bus can replace
or wrap the socket in phase 3; the event vocabulary is the durable part.)

### 6.2 Phased roadmap

**Phase 0 — this week, no architecture (each item is small):**
1. **Earcons**: rising tick on record-start, falling tick on typed, low buzz
   on error, soft pop on wake-ack. `paplay` on bundled OGGs (or
   `canberra-gtk-play`), `ui.sounds = true|false`.
2. **Stop double-reporting**: `deliver()` skips the "✓ …" notification when
   the overlay is live (it already knows via `_ui_live`).
3. **Errors die on stage**: overlay gains a red error state with the reason
   and an 8 s linger — replacing today's silent vanish (`tick_overlay` needs
   an error field in the state file; smallest possible change: write
   `error:<msg>` as a status).
4. **Blocked-before-speaking**: when the last health verdict is DOWN, a
   toggle/wake attempt shows "server unreachable" (overlay red state +
   buzz) instead of starting a doomed recording. The daemon already has
   `_server_ok`.
5. **Double-press = cancel** on the media key (the watcher already sees raw
   events; a 400 ms double-press window), with a descending cancel earcon.

**Phase 1 — the instrument (the visible overhaul):**
6. The event socket (§6.1); delete notification fallbacks from all attended
   paths.
7. Rebuild the overlay as the pill: waveform from `level` events, countdown
   ring, shimmer, ✓/error states, spring grow/shrink; captions as the
   attached card with `always/hover/off`.
8. Dockable position with memory; multi-monitor: follow the monitor with the
   focused window (the daemon can ask the X side via xdotool where focus is).
9. Tray slimmed to mirror-dot + actions + history + settings; drop the five
   borrowed icons for one vani glyph in the four state colors.
10. Never lose speech: on error/cancel, keep the WAV + partial transcript;
    `vani retry` re-streams `last.wav`; the pill's error state offers it.

**Phase 2 — the gestures:**
11. **Hold-to-talk**: press-and-hold (>250 ms) records while held, release
    commits instantly — no silence wait, no countdown. Tap keeps today's
    hands-free flow. (The XI2 watcher already sees press/release; the
    keycode config gains nothing.)
12. Named mic states: Listening (recording) / **Asleep** (wake armed, local
    analysis only) / **Off** (mic closed) / Blocked (server down) across
    pill, tray, `vani status`.
13. "Taking longer than usual" pill state past ~4 s of finishing, with the
    elapsed time; keep the 20 s timeout behind it.
14. Focus echo: the ✓ state names the target ("✓ typed → Firefox") — catches
    wrong-window accidents; on no-focusable-field, say "copied — nothing
    focused to type into" *as the pill state*, not a notification.

**Phase 3 — the platform:**
15. D-Bus interface wrapping the event vocabulary; GNOME Quick Settings
    toggle; then the Shell-extension frontend (top-bar state glyph +
    shell-layer HUD) with the GTK pill auto-yielding when the extension is
    present.
16. Agent groundwork on the same rails: command utterances, routing,
    per-app behavior — the pill's outcome echo becomes the agent's voice.

### 6.3 What this deletes

Worth being explicit, since the brief was "ready to delete everything":

- All notifications during attended sessions (live-text mirroring, countdown
  banners, "✓ typed", "Cancelled", "no speech detected") — replaced by pill
  states + earcons. Notifications remain *only* for: server down/up while
  idle-unattended, and the no-UI-process fallback.
- The five borrowed GNOME icons and the tray-menu status prose.
- The `_ui_live` pidfile inference and the notification-vs-overlay fork.
- The standalone `toggle.py` UX fork (it keeps working headless, but through
  the same event vocabulary → same earcons, same fallback rules).
- The 520 px caption box as the primary surface (it survives demoted, as the
  pill's optional caption card).

---

## Appendix — full research findings

### Wispr Flow (Mac/Windows/iOS; Linux waitlist-only)

- **Visual affordance**: The "Flow Bar" — a dark pill at **fixed bottom-center**, never caret-anchored. Idle bubble → recording state with **live waveform of animated white bars** plus Cancel (X) and Stop (■) buttons ([docs](https://docs.wisprflow.ai/articles/5096240724-navigating-the-wispr-flow-app-desktop-ios-and-android), [hands-free doc](https://docs.wisprflow.ai/articles/6391241694-use-flow-hands-free)). After user backlash it became **draggable to left/right screen edges** (reorients vertically when docked; position remembered). Anti-misclick detail: "the center of the Flow Bar is intentionally not clickable during dictation, so you can't interrupt a session by accident."
- **Activation**: Push-to-talk default — **hold `fn`** (Mac) / `Ctrl+Win` (Windows); release = stop + paste. **Double-tap the PTT key** for hands-free toggle. Command Mode on a third chord. No wake word. A "ping" on record-start, a "paste" sound on completion ("Speak after the ping, or when the white bars move").
- **Live transcript**: **None — deliberately.** Streaming raw ASR means "words arrive as they're spoken, often wrong and overstimulating and jarring"; instead Flow will "wait, understand, and then write what you meant… Streaming might give your writing speed, but Flow gives it clarity and meaning" ([design post](https://wisprflow.ai/post/designing-a-natural-and-useful-voice-interface)).
- **Errors** (the most complete taxonomy found): offline → bar **grays out with muted icon before you start**; connection lost mid-dictation → **audio auto-saved locally, re-transcribed on reconnect** ("Your dictation was interrupted — Tap to view your transcript"); mic problems → distinct strings ("Is your microphone muted?", "Audio is silent", "No microphone detected", "Selected microphone is unavailable") plus a bouncing level meter ([docs](https://docs.wisprflow.ai/articles/2841416128-why-isn-t-flow-recording-my-voice)).
- **Processing delay**: Initializing / Processing / Stopping bar states; pulsing loader during reconnection; "Taking longer than usual" toast with "Your audio is saved for retrying" ([doc](https://docs.wisprflow.ai/articles/4984532368-fix-taking-longer-than-usual-and-transcription-errors)).
- **Patterns**: Esc cancels (audio still saved to Recent activity); 20-min hands-free cap with a 19-min warning; **no auto-stop-on-silence in hands-free** — stop is always explicit; self-correction ("X… I meant Y" outputs only Y); "Mute music while dictating" ducking option.
- **Latency (published)**: **700 ms end-to-end** from end-of-speech to formatted text (ASR < 200 ms, LLM formatting < 200 ms, network 200 ms) — "Any slower, and users get impatient" ([technical post](https://wisprflow.ai/post/technical-challenges)).
- **Philosophy**: "Voice is invisible, and that's its superpower… We don't want Flow to be the thing on your mind all the time." Rejected both "speaking into a void" and "constantly flashing an obstructive UI" for "a small, persistent bar… that signals availability without noise." "Flow should disappear into your muscle memory."

### Aqua Voice (Mac/Windows/iOS)

- **Black floating pill at bottom**, fixed, **fully disableable** (zero-UI mode supported) ([FAQ](https://aquavoice.com/info/faq)).
- Hold **`Fn`** / right `Alt`; "Recording happens only while the activation key is held down, and text is inserted immediately upon release." Rebindable incl. **mouse side buttons**. Double-press = hands-free Realtime mode.
- **Streams partial text inline into the target app** and refines it in place; non-final tokens "resolve upon commitment" ([Launch HN](https://news.ycombinator.com/item?id=39828686)). "Starts up in under 50ms, inserts text in about a second (sometimes as fast as 450ms)" ([Aqua 2 Show HN](https://news.ycombinator.com/item?id=43634005)).
- **Local History keeps transcripts and audio locally "so you never lose text."**
- **Edit Mode via selection state** — hold the same key with text selected and it becomes a voice-edit command ("fix the grammar," "make it shorter") — mode inferred from context. **"Send it"** intent-aware submit command.

### superwhisper (Mac)

- Recording Window with full and **mini-pill modes**; real-time waveform (a *static* waveform is the documented tell for mic problems); a **4-color status dot** as the entire state language: **yellow = model loading, red = recording, blue = processing, green = done**, mirrored identically in the menu-bar icon ([docs](https://superwhisper.com/docs/get-started/interface-rec-window.md)). The overlay can be **disabled entirely "while maintaining audio cues."**
- Default Option+Space; **tap = toggle, press-and-hold = push-to-talk** on the same shortcut; mouse buttons supported.
- Realtime streaming text gated to the cloud model; default is waveform feedback, text arrives after the blue-dot phase.
- Since v2.15 warns "when no audio is detected"; cancel prompts confirmation over 30 s; history exposes per-stage timing (transcription vs AI). Start cue + completion tone, volume independent of system volume, **switchable sound themes**.
- **No auto-stop on silence** — "silence removal" strips quiet stretches instead. Opt-in clipboard restore ~3 s after paste (default overwrite is a top complaint). Shipped **focus-stealing fixes twice** (v1.31.2, v2.16.4).

### MacWhisper (Mac)

- Dictation indicator **position user-choosable: center-top, center-bottom, "textfield location" (caret-anchored), or hidden** — the most flexible placement model found ([release notes](https://macwhisper-site.vercel.app/release_notes.html)). Overlay doubles as error surface ("If a blank dictation is detected, show an error in the overlay").
- Bare-modifier PTT (Fn or right ⌘) plus toggle; Escape cancels.
- Chunked processing masked cleverly: "your words are transcribed in the background as you speak" so the final insert on release feels instant.
- "Dictation errors are now displayed in your dictation history"; last-50-dictations menu as safety net; iterated toward **subtler sound effects** (v13.4); "Mute Audio During Dictation" toggle.

### Apple macOS Dictation

- State anchored to **the insertion point itself**: "When the cursor is highlighted and pulsing, or you hear the tone… dictate"; WWDC23 added a trailing glow while speaking and a scroll-aware mic indicator (`NSTextInsertionIndicator`) ([WWDC23](https://developer.apple.com/videos/play/wwdc2023/10058/)). Trajectory: floating meter panel → in-line caret-anchored glow.
- Toggle; mic key (press = Dictation, **hold = Siri**); Esc stops. Auto-stops after **30 s of silence**; failures mostly silent.
- Words typed inline immediately and **retroactively revised** — "changing a word back-and-forth between two possibilities as you continue speaking" ([TidBITS](https://tidbits.com/2020/08/31/how-ios-and-macos-dictation-can-learn-from-voice-controls-dictation/)); low-confidence words get a blue underline with alternatives.
- Start/end chimes; complaints the beep can't be disabled post-Catalina.

### iOS Dictation (16+)

- Keyboard stays on screen; **tappable mic bubble next to the text cursor** + trailing glow; "a quiet 'boop' sound… tells you the system's listening." **Modeless** — typing and dictating concurrent. The start "boop" **plays even on silent** — an audible privacy contract.
- Criticisms: retroactive-correction jank ("watching the iPhone type words and then change them right before sending"); the "Trump"/"racist" transient bug was provisional hypotheses styled as final text; iOS 18 pause detection "sometimes stopping mid-sentence if you pause to think."

### Windows 11 Voice Typing (Win+H)

- Floating rounded flyout (round mic button, gear, X), repositionable, not caret-anchored. Explicit "**Listening…**" label — "Wait for the 'Listening...' alert before you start speaking" ([Microsoft](https://support.microsoft.com/en-us/windows/use-voice-typing-to-talk-instead-of-type-on-your-pc-fec94565-c4bd-329d-e59a-af033fa5689f)). Opt-in launcher auto-shows the flyout when focus enters a text box.
- **Fails verbosely**: "Something happened and we couldn't enter your voice typing text"; "There was a connection issue"; prompts when no text box is focused.
- No sounds — which makes its aggressive stops easy to miss: **~5–10 s silence timeout, and any keypress or focus change ends the session by design.**

### Windows Voice Access

- **Full-width bar docked at top of screen**: mic state, **live echo of what you said**, command execution status, current mode.
- Three explicit mic states — **Listening / Sleep / Off** — with wake phrases ("Voice access wake up") and hotkeys. **It never times out; it sleeps.** The most legible answer to "why did it stop?" found anywhere.
- Three modes (Commands / Dictation / Default) switchable by voice, mode always shown on the bar. Number overlays and grid for mouse targeting; "show keyboard" for passwords so secrets bypass the dictionary.

### Talon Voice

- Shipped UI ≈ nothing: tray icon + log. The community built **talon_hud** (status bar changing look per mode + an **event log of recognized commands that clears after a few seconds**), mode-indicator dots, per-phrase notifications, even hardware mode switches ([talon_hud](https://github.com/chaosparrot/talon_hud)) — a documented record of exactly what feedback users missed: visible mode state, per-utterance echo. "Wayland support is not planned."

### ChatGPT voice mode

- Pre-Nov-2025: full-screen glowing orb, no transcript. **The Nov 2025 redesign moved voice inline into the chat with a live transcript** — the orb survives only as opt-in "Separate Mode." Even for a conversational agent, users rejected the opaque orb in favor of visible words. "Connection failed, tap to retry" after ~10 s. Chimes cannot be disabled (standing complaint).

### Linux tools

- **whisper-overlay** (oddlama): the Linux reference for live feedback — hold Right Ctrl (evdev), a **layer-shell overlay shows streaming live transcription from a fast model, then an accurate model revises the draft on release** before injection via virtual-keyboard-v1. Waybar module: gray disconnected / blue connected / red recording. wlroots-only — does not run on GNOME ([repo](https://github.com/oddlama/whisper-overlay)).
- **Handy**: tray + recording overlay; its own README warns "the recording overlay window can interfere with pasting transcribed text into target applications" — recommended workaround: overlay position "None" + **Audio Feedback on**. Visual feedback breaking the core function via focus.
- **nerd-dictation**: zero UI by design; HN verdict: accuracy good but "it doesn't do punctuation, new lines, capitalization."
- **Speech Note**: Wayland insertion requires an external ydotool daemon with correct socket perms — the classic silent-failure setup step.
- Cross-cutting Linux complaints: delay breaking flow; requests for "a visual icon with waves etc showing recording"; the Wayland tax on hotkeys/typing/overlays.

### HCI grounding

- **Calm technology**: the interface should "engage both the center and the periphery of attention, and move back and forth between the two" ([overview](https://www.numberanalytics.com/blog/calm-tech-hci-philosophy); [Interaction-Attention Continuum](http://www.ijdesign.org/index.php/IJDesign/article/view/2341/737)).
- **Peripheral vision**: very low acuity (text is useless off-fovea) but **highly sensitive to motion and color change** ([Human Vision for UI Designers](https://www.breck-mckye.com/blog/2012/08/human-vision-for-ui-designers/); [Matthews & Forlizzi, glanceable peripheral displays](https://digitalassets.lib.berkeley.edu/techreports/ucb/text/EECS-2006-113.pdf)). Peripheral errors are missed — errors must move to the focus point, use sound, or persist.
- **Latency**: 100 ms feels self-caused; 1 s is the limit for uninterrupted thought ([Nielsen](https://www.nngroup.com/articles/response-times-3-important-limits/)). Human turn-taking gaps: ~0–300 ms, median ~200 ms (Stivers et al.); felt unconsciously past ~300 ms, noticed past ~500 ms ([Hamming](https://hamming.ai/resources/voice-ai-latency-whats-fast-whats-slow-how-to-fix-it)).
- **Earcons**: best as a small consistent vocabulary (2–3 sounds) for state transitions; rising = start, falling = stop; always disableable ([NN/g audio signifiers](https://www.nngroup.com/articles/audio-signifiers-voice-interaction/)).
- **Quasimodes**: a mode maintained by constant physical action cannot be forgotten — "you cannot forget you are holding shift and then be surprised by capitals" ([The Humane Interface](https://en.wikipedia.org/wiki/Quasimode_(computer_interface))). Toggle dictation is a latched mode (Caps Lock) and produces classic mode errors; if you must latch, name the states and show them constantly (Voice Access).
- **Trust/privacy**: cloud-only processing is the single biggest backlash source (Wispr Trustpilot 2.7/5; [forensic teardown](https://wensenwu.com/thoughts/wispr-flow-investigation) found system-wide keystroke tap + accessibility scraping). Be loud and legible about what leaves the machine; keep raw audio local.

