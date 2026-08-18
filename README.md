# vani

**Voice dictation for the Linux desktop.** Say the wake word or tap a key, talk,
stop talking — the text is typed into whatever field has focus.

Speech recognition runs on a remote GPU (Voxtral); vani is the local client:
wake-word spotting, hotkey handling, recording, silence detection, and typing.
Wake-word spotting happens entirely on your machine — audio only leaves it once
a recording has actually started.

```
$ vani doctor
  ✓ config          ~/.config/vani/config.toml
  ✓ server url      wss://ai-stream.lsquarelabs.com/v1/realtime
  ✓ arecord         /usr/bin/arecord
  ✓ typing backend  xdotool
  ✓ wake model      ~/.local/share/vani/vosk-model-small-en-us-0.15
  ✓ websockets
  ✓ daemon          running (pid 40122)
  ✓ start on login  enabled
  ✓ server          wss://ai-stream.lsquarelabs.com/v1/realtime
```

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

Requirements: Linux, Python 3.9+, X11 (see [Wayland](#wayland) below). The only
Python dependency is `websockets` (the transcription transport) — recording is
an `arecord` pipe, health checks are `urllib`. `vosk` is optional and only
powers the wake word.

## Using it

Three ways to start a recording; all of them stop the same way — pause for
three seconds and the clip is sent.

| | |
|---|---|
| **Wake word** | say *"hey claude"* (configurable) |
| **Media key** | press the key the daemon watches (keycode 171 = `XF86AudioNext`) |
| **Push-to-talk** | bind `vani toggle` to any shortcut in your desktop settings |

While it counts down, speaking again cancels the send and recording continues.
Pressing the key during a recording sends it immediately.

While you speak, the audio streams to the realtime endpoint and the text
appears live in the notification, a word or two behind your voice. Nothing is
typed until the recording ends — then the final transcript goes into the
focused field in one piece, so there is never anything to un-type.

The daemon checks the server's health at startup and every few minutes, and
tells you — once, not per recording — when it becomes unreachable and when it
is back. The tray shows the current verdict; so does `vani status`.

### Commands

| Command | What it does |
|---|---|
| `vani start` | run the daemon in the foreground (systemd normally does this) |
| `vani toggle` | start/stop a recording — the daemon's if it's running, else standalone |
| `vani tray` | tray indicator: state, server health, transcripts, settings |
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
| `server.url` | `wss://ai-stream.lsquarelabs.com/v1/realtime` | realtime ASR WebSocket |
| `server.token` | — | optional Bearer token; or `server.token_file`, or `$VANI_TOKEN` |
| `server.model` | `mistralai/Voxtral-Mini-4B-Realtime-2602` | model named in `session.update` |
| `server.timeout_sec` | `20` | wait for the final transcript after a recording ends |
| `server.health_check_min` | `5` | minutes between connectivity checks (0 = off) |
| `wake.enabled` | `true` | set false for hotkey-only (no vosk needed) |
| `wake.phrases` | `["hey claude", "hi claude"]` | words must exist in the Vosk vocabulary |
| `wake.model_dir` | `~/.local/share/vani/vosk-model-small-en-us-0.15` | model location |
| `recording.device` | — | pinned microphone (`vani mic`); empty = system default |
| `recording.silence_sec` | `3` | silence that ends a recording |
| `recording.silence_warn_sec` | `1` | silence before the countdown appears |
| `recording.max_sec` | `120` | hard limit on one recording |
| `recording.speech_factor` | `3.5` | how far above the noise floor counts as speech |
| `recording.min_speech_level` | `350` | absolute bar for speech; lower for a quiet mic |
| `hotkey.enabled` / `.keycode` | `true` / `171` | the watched key |
| `output.typer` | `auto` | `xdotool`, `ydotool`, `clipboard`, `stdout` |
| `output.notify` / `.history` | `true` | desktop notifications / transcript log |

Unknown or mistyped keys are rejected at startup rather than silently ignored —
except the keys the batch-era app wrote itself (`server.endpoint`,
`recording.auto_gain`, an `https://` server URL), which are migrated in place
so an upgrade never strands the config.

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
