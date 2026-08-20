# vani

**Voice dictation for the Linux desktop.** Say the wake word or tap a key, talk,
stop talking — the text is typed into whatever field has focus.

Speech recognition runs on a hosted realtime ASR service; vani is the local
client: wake-word spotting, hotkey handling, recording, silence detection, and
typing. Wake-word spotting happens entirely on your machine — audio only
leaves it once a recording has actually started.

Two services are supported, chosen by `server.provider`:

| Provider | Endpoint | Auth |
|---|---|---|
| **`deepgram`** (default) | `wss://api.deepgram.com/v1/listen` | API key, `Authorization: Token …` |
| `voxtral` | an OpenAI-realtime-shaped vLLM server | optional Bearer token |

`provider = "auto"` reads it off the URL, so pointing `server.url` at
`api.deepgram.com` is enough.

```
$ vani doctor
  ✓ config          ~/.config/vani/config.toml
  ✓ api token
  ✓ provider        deepgram
  ✓ server url      wss://api.deepgram.com/v1/listen
  ✓ model           nova-3
  ✓ arecord         /usr/bin/arecord
  ✓ typing backend  xdotool
  ✓ wake model      ~/.local/share/vani/vosk-model-small-en-us-0.15
  ✓ websockets
  ✓ daemon          running (pid 40122)
  ✓ start on login  enabled
  ✓ server          wss://api.deepgram.com/v1/listen
```

### The API key

Put it in `~/.config/vani/config.toml` (mode 600) — that is the only place the
systemd-managed daemon will find it:

```toml
[server]
provider = "deepgram"
url = "wss://api.deepgram.com/v1/listen"
model = "nova-3"
token = "…"
```

`server.token_file` and `$DEEPGRAM_API_KEY` work too; the env var only reaches
the daemon if you export it into the systemd user environment, so the config
file is the reliable option. `vani doctor` verifies the key by calling
Deepgram's project listing — a rejected key is caught there, not twenty
seconds into your first recording.

## Install / uninstall

```sh
git clone https://github.com/sudhirpratapyadav/vani.git && cd vani
./install.sh
```

That installs the package into `~/.local/bin`, writes a config (importing the
old `~/.config/dictate/config` if it's still there), downloads the 40 MB
wake-word model, and enables the systemd user services — the daemon and the
tray start now and on every login. `vani doctor` at the end confirms the
server is reachable.

To remove vani, `./uninstall.sh` stops and disables the services, removes the
units, and uninstalls the package; add `--purge` to also delete the config,
history, and wake-word model.

Manually, if you'd rather not run the script:

```sh
sudo apt install alsa-utils xdotool xinput          # recording, typing, key events
pipx install .          # or: python3 -m pip install --user .
pip install --user vosk # optional: wake words
vani config init && vani model download
cp systemd/*.service ~/.config/systemd/user/
systemctl --user enable --now vani-daemon.service vani-tray.service
```

Requirements: Linux, Python 3.8+, X11 (see [Wayland](#wayland) below). The only
Python dependency is `websockets` (the transcription transport) — recording is
an `arecord` pipe, health checks are `urllib`. `vosk` is optional and only
powers the wake word.

## Using it

Ways to start a recording:

| | |
|---|---|
| **Wake word** | say *"hey claude"* (configurable) — hands-free |
| **Tap the key** | tap the watched key (keycode 171 = `XF86AudioNext`) — hands-free |
| **Hold the key** | hold it and talk; **release sends immediately** |
| **Shortcut** | bind `vani toggle` to any shortcut in your desktop settings |

One key, two gestures. A **tap** starts a hands-free session that ends when you
pause for three seconds — speaking again during the countdown cancels the send
and keeps recording, and tapping again sends at once. A **hold** (longer than
`hotkey.hold_sec`) is push-to-talk: it records while held and commits the
moment you let go, with no silence wait at all — the fastest path from thought
to text. Holding the key *during* a hands-free recording cancels it instead.

### The pill

Everything about a live session appears in one place: a small pill that grows
out of the idle dot the instant recording starts.

```
 Off        nothing on screen (the tray icon shows a muted mic)
 Asleep     a dim dot — wake word armed
 Blocked    an amber dot — the server is unreachable, so a key press
            would be refused; you know before you speak, not after
 Listening  a red dot and a live waveform; a ring around the dot means
            push-to-talk, so releasing the key will send
 Countdown  the dot becomes a draining ring — the seconds until it types
 Finishing  the waveform freezes and a shimmer sweeps the pill; past four
            seconds it says "taking longer than usual" with a counter
 Typed      "✓ typed → Firefox" — the window that actually received it
 Error      the pill turns red, says why in one sentence, and offers ↻ retry
```

The live draft grows in a card attached to the pill (`ui.captions`: `always`,
`hover`, or `off`). Nothing is typed until the recording ends — the final
transcript goes into the focused field in one piece, so there is never
anything to un-type. The pill docks to any of six positions and follows the
monitor holding the focused window.

Every state also has a sound — a pop when the wake word lands, a rising tick
when the mic goes hot, a falling tick when the text is typed, a low buzz for
trouble — because dictation is an eyes-busy activity. Turn them off with
`ui.sounds = false`.

Desktop notifications are the fallback for when the tray process is not
running, not a second channel alongside the pill.

`vani cancel` — the tray's "Cancel", the ✕ on the pill, or holding the key
while recording — throws the recording away instead of typing it.

### Sending, not just typing

`output.submit = true` (tray: **Settings → Press Enter after typing**) presses
Enter once the transcript is in, so a chat box, a shell prompt, or a search
field acts on what you said instead of just holding it — dictate a prompt and
it runs. The pill says "✓ sent → Firefox" rather than "✓ typed" when it did.

It is off by default, because in a text editor it inserts a newline you did
not ask for. The Enter is a separate keypress, not a newline in the typed
string — a chat box treats those differently. It needs a key-pressing backend:
there is nothing to submit on the clipboard, and `vani doctor` shows which
backend you have. The tray toggle takes effect immediately; no restart.

### False wakeups

Vosk decodes against a grammar of just the wake phrases, so anything that
rhymes gets forced onto the nearest one — its *partial* results flicker
through "hey claude" on the way to "hey [unk]" for a phrase like "hey
Claudia" or "hey there, could you". vani therefore requires a phrase seen in
a partial to still be there `wake.confirm_sec` later (0.25 s by default); a
phrase in a *final* result wakes immediately, since the decoder has already
settled. Measured on synthesised speech, that took false wakeups from 2 in 10
to 0 in 10 while still catching every real one, for about 0.25 s of extra
latency. Set `wake.confirm_sec = 0` for the old instant behaviour, or raise it
if your wake phrase sounds like something you say often.

### Nothing said is ever lost

The last clip is always kept, including one you cancelled or one whose
transcription failed, and `vani retry` (or the pill's ↻ button) sends it
again. If the stream dies mid-sentence, the words it had already returned are
kept too — flagged `[not typed]` in `vani history`. If the text cannot be
typed into the focused window, it is copied to the clipboard instead.

`vani disable` (tray: "Disable dictation") closes the microphone entirely:
no wake-word spotting, no key, nothing captured rather than
captured-and-ignored — the same privacy stance as quitting, without losing
the daemon. The tray icon shows a muted mic; `vani enable` resumes. Key
presses made while disabled are dropped, not queued.

The daemon checks the server's health at startup and every few minutes, and
tells you — once, not per recording — when it becomes unreachable and when it
is back. The tray shows the current verdict; so does `vani status`.

### Commands

| Command | What it does |
|---|---|
| `vani start` | run the daemon in the foreground (systemd normally does this) |
| `vani toggle` | start/stop a recording — the daemon's if it's running, else standalone |
| `vani cancel` | discard the current recording; nothing is typed |
| `vani retry` | re-send the last clip — after a failure, or a cancel you regret |
| `vani disable` / `enable` | close/reopen the microphone — disabled means nothing is captured at all |
| `vani tray` | the UI process: tray indicator + live-caption overlay |
| `vani status` | daemon state, server connectivity, last transcript |
| `vani service status\|start\|stop\|restart\|enable\|disable` | manage the background services |
| `vani mic [list\|set N\|test]` | pick the microphone; `test` records 3 s and transcribes it |
| `vani quit` | stop vani completely — daemon and tray |
| `vani history [-n 20]` | past transcripts |
| `vani doctor` | check every dependency, the config, and the server |
| `vani config init\|show\|edit\|path` | manage the config file |
| `vani model download` | fetch the wake-word model |
| `vani say file.wav` | transcribe a WAV from the command line |
| `vani start --test-wav f.wav` | replay a WAV through the state machine, no mic or network |

## How it works

```
mic ──> arecord ──> vani daemon
                      │
                      ├── idle: Vosk keyword-spotting on a grammar of just the
                      │   wake phrases (local, ~40 MB model), plus an ambient
                      │   noise-floor average
                      │
                      ├── recording: buffer until 3 s below the adaptive speech
                      │   threshold; countdown shows after 1 s; speaking again
                      │   cancels it; the key sends immediately
                      │
                      ├── while recording, every chunk streams over a WebSocket
                      │   to wss://ai-stream.lsquarelabs.com/v1/realtime and the
                      │   deltas show live in the notification
                      │
                      └── on stop: commit the stream, take the final text
                            └─ xdotool types it into the focused field
                               and appends to ~/.cache/vani/history.log

          every 5 min ── GET https://ai-stream…/health ──> one banner when the
                         server goes away, one when it is back
```

Design notes worth knowing before changing things:

- **The silence threshold adapts, in two directions.** A fixed threshold works
  at a desk and fails on a train, so the noise floor is tracked as an
  exponential average while idle and speech is anything ~3.5× louder. The hard
  floor under that (`recording.min_speech_level`) assumes a normally scaled
  microphone, which is not safe: a Bluetooth headset in HFP mode delivers
  speech peaking near RMS 430, so most of a sentence fell under the old fixed
  350 and recordings were sent mid-sentence. The floor therefore also scales
  down to a quarter of the loudest speech actually heard, which leaves loud
  microphones behaving exactly as before.
- **The media key is read from raw X input events**, not a desktop shortcut. A
  GNOME custom shortcut on an XF86 media keysym silently never fired, and
  `XGrabKey` delivered exactly one event; `xinput test-xi2 --root` has been
  reliable. The daemon sweeps orphaned `xinput` watchers at startup, because a
  stale one holds the XI2 selection and the next one gets `BadAccess`.
- **The microphone is reopened after every send.** Otherwise the pipe replays
  whatever accumulated during the transcription round-trip into the next
  recording.
- **There is no batch fallback.** The stream is the only transcription path;
  when it fails the recording is reported as failed and the health monitor
  answers "is the server down?". One path means one thing to debug.
- **`vani toggle` signals the running daemon** (SIGUSR1) rather than opening a
  second recorder, so one process owns the microphone.

Layout: `session.py` is the state machine and knows nothing about microphones,
sockets, or the desktop — that's what makes it testable offline. `daemon.py`
wires it to `audio.py`, `wake.py`, `hotkey.py`, `stream.py` (the transcription
socket), `client.py` (health), `service.py` (systemd), and `output.py`.

## Configuration

`~/.config/vani/config.toml` (mode 600 — it may hold a token). `vani config
show` prints the effective values with the token masked.

| Key | Default | Meaning |
|---|---|---|
| `server.provider` | `auto` | `auto`, `deepgram`, or `voxtral`; `auto` reads it off the URL |
| `server.url` | `wss://api.deepgram.com/v1/listen` | realtime ASR WebSocket |
| `server.token` | — | API key; or `server.token_file`, `$VANI_TOKEN`, `$DEEPGRAM_API_KEY` |
| `server.model` | `nova-3` | Deepgram `?model=`, or the name in Voxtral's `session.update` |
| `server.timeout_sec` | `20` | give up when the server goes quiet this long after a recording ends (activity resets it) |
| `server.health_check_min` | `5` | minutes between connectivity checks (0 = off) |
| `wake.enabled` | `true` | set false for hotkey-only (no vosk needed) |
| `wake.phrases` | `["hey claude", "hi claude"]` | words must exist in the Vosk vocabulary |
| `wake.model_dir` | `~/.local/share/vani/vosk-model-small-en-us-0.15` | model location |
| `wake.confirm_sec` | `0.25` | how long a phrase must persist before it counts — the false-wakeup defence; `0` restores the old instant trigger |
| `recording.device` | — | pinned microphone (`vani mic`); empty = system default |
| `recording.silence_sec` | `3` | silence that ends a recording |
| `recording.silence_warn_sec` | `1` | silence before the countdown appears |
| `recording.max_sec` | `120` | hard limit on one recording |
| `recording.speech_factor` | `3.5` | how far above the noise floor counts as speech |
| `recording.min_speech_level` | `350` | absolute bar for speech; lower for a quiet mic |
| `hotkey.enabled` / `.keycode` | `true` / `171` | the watched key |
| `hotkey.hold_sec` | `0.35` | hold this long and the press becomes push-to-talk |
| `hotkey.cancel_hold_sec` | `0.6` | holding this long during a hands-free recording cancels it |
| `ui.enabled` | `true` | the pill and its caption card |
| `ui.position` | `bottom-center` | pill dock: `bottom`/`top` × `left`/`center`/`right` |
| `ui.captions` | `always` | live draft card: `always`, `hover` (only under the pointer), `off` |
| `ui.idle_dot` | `true` | the dim dot while asleep; off = nothing on screen when idle |
| `ui.sounds` | `true` | earcons for start, typed, and trouble |
| `ui.width` / `.max_height` | `520` / `260` | caption card size in px; height grows to the cap, then scrolls |
| `ui.opacity` | `0.88` | pill and card background opacity (0.2–1.0) |
| `output.typer` | `auto` | `xdotool`, `ydotool`, `clipboard`, `stdout` |
| `output.submit` | `false` | press Enter after the text, so prompts and chat boxes act on it |
| `output.notify` / `.history` | `true` | fallback notifications / transcript log |
| `output.save_last_wav` | `true` | keep the last clip so `vani retry` can resend it |

Unknown or mistyped keys are rejected at startup rather than silently ignored —
except the keys older versions wrote themselves (`server.endpoint`,
`recording.auto_gain`, `recording.transport`, the interim `[stream]` section,
an `https://` server URL), which are migrated in place so an upgrade never
strands the config.

Find your key's keycode with:

```sh
xinput test-xi2 --root | grep -A2 RawKeyPress
```

## Files

```
~/.config/vani/config.toml     configuration (600)
~/.local/share/vani/           wake-word model
~/.cache/vani/history.log      transcripts
~/.cache/vani/last.wav         last clip recorded, for debugging
$XDG_RUNTIME_DIR/vani/         status, server verdict, pidfiles (volatile)
```

`./uninstall.sh --purge` removes all of it.

## Troubleshooting

Start with `vani doctor` — most failures here are environmental and it names
them directly. Then:

- **"✗ Transcription failed" / server DOWN in the tray** — the server or the
  path to it is gone; `vani doctor` names which. The daemon re-checks every
  few minutes and posts a banner when it is back. See
  [Server side](#server-side) for bringing the GPU end up.
- **Live text works but nothing is heard / clips are discarded** — the wrong
  microphone is being recorded. `vani mic test` answers it in one command;
  `vani mic set` pins the right one (tray → Settings → Microphone does the
  same). A Bluetooth headset's mic only exists in its headset profile — vani
  switches the profile automatically when that mic is selected, which also
  means playback drops to call quality while the daemon holds the mic open.
- **"(no speech detected)"** — listen to `~/.cache/vani/last.wav`: it is the
  same audio the server heard. A wired or built-in mic (Settings → Sound →
  Input) beats a Bluetooth headset for ASR by a wide margin.
- **The media key stopped working**, `BadAccess` in the log — an orphaned
  watcher: `pkill -f "xinput test-xi2"`, then restart the daemon.
- **Nothing gets typed** — check `vani doctor`'s typing-backend line. On X11
  that needs `xdotool`. On Wayland, if the transcript reaches `vani history`
  but no text appears and the log shows no error, the typing tool is lying
  about success — see [Wayland](#wayland) for the ydotool version trap.
- **Recordings cut off mid-sentence** — raise `recording.silence_sec`, or the
  room is loud enough that the adaptive threshold is treating speech as silence;
  `vani start` in a terminal logs the countdown decisions. If the microphone is
  a very quiet one (a Bluetooth headset in HFP mode, say — `vani doctor` and the
  daemon log both name the device), speech may be sitting under
  `recording.min_speech_level`; lower it and see [How it works](#how-it-works).
- **Logs** — `journalctl --user -u vani-daemon -f` (the daemon logs to the
  journal; see `systemd/vani-daemon.service` for why not to a file).

### Wayland

The media-key watcher is X11-only. Typing falls back to `ydotool` (which needs
its own daemon and `/dev/uinput` access) and then to the clipboard, so
transcripts survive even where they can't be typed. Wake words and push-to-talk
via a desktop shortcut work either way.

For typing you need ydotool **1.0 or newer**, and a daemon to go with it.
Ubuntu 22.04 ships 0.1.8, which does not work: `ydotoold` segfaults the moment
a client connects (`status=11/SEGV` in the journal), and without it `ydotool`
falls back to writing `/dev/uinput` directly, injects nothing, and still exits
0 — so vani logs a transcript and types nothing, with no error anywhere. Check
with `apt-cache policy ydotool`; if that says 0.1.8, build it:

```sh
sudo apt install cmake scdoc build-essential
git clone --depth 1 -b v1.0.4 https://github.com/ReimuNotMoe/ydotool
cmake -S ydotool -B ydotool/build -DCMAKE_BUILD_TYPE=Release
cmake --build ydotool/build -j"$(nproc)" && sudo cmake --install ydotool/build
```

That lands in `/usr/local/bin`, ahead of any apt copy. Then:

```sh
sudo usermod -aG input "$USER"         # /dev/uinput is root:input (re-login)
cp systemd/ydotoold.service ~/.config/systemd/user/
systemctl --user enable --now ydotoold
```

`ydotool type` should then say *Using ydotoold backend*. To confirm keystrokes
really reach the compositor — without depending on which window has focus —
send a key the desktop handles globally and watch it act:

```sh
pactl get-sink-volume @DEFAULT_SINK@   # note the level
ydotool key 115:1 115:0                # volume up; the level should change
```

For the key, bind `vani toggle` to a GNOME shortcut — a normal keysym, not an
XF86 media key, which GNOME accepts and then silently never fires:

```sh
P=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom0/
gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings "['$P']"
gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$P \
    name 'vani dictation toggle'
gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$P \
    command "$HOME/.local/bin/vani toggle"
gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$P \
    binding '<Super><Alt>d'
```

## Development

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -t tests   # no mic or network
PYTHONPATH=src python3 -m vani doctor            # run from the source tree
vani start --test-wav clip.wav                   # replay audio through the state machine
```

Build a test clip with gTTS + ffmpeg: wake phrase → speech → ≥3 s silence
should print `started (wake word)`, a countdown, and `finished (3s silence)`.

## Server side

The GPU server is vLLM serving Voxtral-Mini-Realtime, run inside a Slurm
GPU-holder job on the ihub cluster (dgx1/dgx2). Always launch it through the
wrapper — never `vllm serve` bare:

```sh
bash ~/use_instructions/run_in_holder.sh <HOLDER_JOBID> <GPU> \
    ~/sudhir/voice_api/vllm_realtime.log \
    bash ~/sudhir/voice_api/run_vllm_realtime.sh
```

The wrapper holds the launch incantation (`cuda-compat` first on
`LD_LIBRARY_PATH`, `HF_HOME`) plus two memory guardrails that exist because
vLLM/torch workers have repeatedly taken the whole node down by transiently
allocating ~2 TB of RAM (dmesg shows global OOMs with that signature in
Oct 2025, Jan 2026, Feb 2026, Aug 2026 — Slurm here puts no memory cgroup
around steps, so nothing else stops it):

- `ulimit -v` caps the address space at 200 GiB — ~20× the ~9 GiB this server
  actually uses. A runaway allocation now dies inside vLLM (and the desktop
  app reports "transcription failed / server down") instead of OOM-killing
  the node for everyone.
- `oom_score_adj = 1000` volunteers the server to the OOM killer first if the
  node runs out of memory for any other reason.

Two build traps, each worth an afternoon: `cuda-compat` must be first on
`LD_LIBRARY_PATH` (the node driver reports CUDA 12040, which torch rejects),
and the three flashinfer packages must all be 0.6.13, installed with
`--no-deps` so pip cannot swap torch out from under the cu13 build.

Internet path: `dgx?:8001` → `ssh_bridge.sh` reverse-forward (login node) →
VPS `untu_vps` → cloudflared named tunnel → `ai-stream.lsquarelabs.com`.
cloudflared cannot run on the cluster itself (edge port 7844 is blocked).
**If the holder moves to a different node, update the node name in
`ssh_bridge.sh` and restart it** — a 502 from the tunnel with vLLM running
usually means the bridge still points at the old node.

Endpoints: `GET /health` (vLLM's own, no auth — what the app's health monitor
polls) and the OpenAI-realtime WebSocket at `/v1/realtime`.

## License

MIT — see [LICENSE](LICENSE).
