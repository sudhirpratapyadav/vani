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
  ✓ api token
  ✓ arecord         /usr/bin/arecord
  ✓ typing backend  xdotool
  ✓ wake model      ~/.local/share/vani/vosk-model-small-en-us-0.15
  ✓ daemon          running (pid 40122)
  ✓ server          https://ai.lsquarelabs.com {'ready': True}
```

## Install

```sh
git clone https://github.com/sudhirpratapyadav/vani.git && cd vani
./install.sh
```

That installs the package into `~/.local/bin`, writes a config (importing the
old `~/.config/dictate/config` if it's still there), downloads the 40 MB
wake-word model, and enables the systemd user service. Then put your API token
in `~/.config/vani/config.toml` and restart:

```sh
systemctl --user restart vani-daemon
vani doctor
```

Manually, if you'd rather not run the script:

```sh
sudo apt install alsa-utils xdotool xinput          # recording, typing, key events
pipx install .          # or: python3 -m pip install --user .
pip install --user vosk # optional: wake words
vani config init && vani model download
cp systemd/*.service ~/.config/systemd/user/
systemctl --user enable --now vani-daemon.service
```

Requirements: Linux, Python 3.9+, X11 (see [Wayland](#wayland) below). The core
client has no Python dependencies — recording is an `arecord` pipe, HTTP is
`urllib`. `vosk` is optional and only powers the wake word.

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

### Commands

| Command | What it does |
|---|---|
| `vani start` | run the daemon in the foreground (systemd normally does this) |
| `vani toggle` | start/stop a recording — the daemon's if it's running, else standalone |
| `vani tray` | tray indicator: state icon, last 5 transcripts, click to copy |
| `vani status` | what the daemon is doing right now |
| `vani history [-n 20]` | past transcripts |
| `vani doctor` | check every dependency, the config, and the server |
| `vani config init\|show\|edit\|path` | manage the config file |
| `vani model download` | fetch the wake-word model |
| `vani say file.wav` | transcribe a WAV from the command line |
| `vani start --test-wav f.wav` | replay a WAV through the state machine, no mic or network |
| `vani listen` | **v2 (in progress)** — stream ambient speech to a rolling transcript |
| `vani listen --tail` | follow that transcript live |

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
                      └── POST WAV ──> https://ai.lsquarelabs.com/transcribe
                                           │  (Bearer token)
                            text  <────────┘
                            └─ xdotool types it into the focused field
                               and appends to ~/.cache/vani/history.log
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
  whatever accumulated during the HTTP round-trip into the next recording.
- **Quiet audio is amplified before sending.** Bluetooth headsets in HFP mode
  record narrowband and very quietly, and the model transcribes that as empty.
- **`vani toggle` signals the running daemon** (SIGUSR1) rather than opening a
  second recorder, so one process owns the microphone.

Layout: `session.py` is the state machine and knows nothing about microphones,
HTTP, or the desktop — that's what makes it testable offline. `daemon.py` wires
it to `audio.py`, `wake.py`, `hotkey.py`, `client.py`, and `output.py`.

## v2 — the ambient agent

An always-listening sibling to dictation is being built alongside it, described
in [docs/v2-design.md](docs/v2-design.md). Phase 1 — the ear — works today:

```sh
pip install --user websockets          # the only new dependency
vani listen                            # start listening
vani listen --tail                     # watch the transcript fill up
vani listen --test-wav clip.wav -v     # replay a WAV, no microphone needed
```

Speech goes to a realtime Voxtral on the GPU over a WebSocket and comes back
word by word, ~0.4 s behind. A coarse voice-activity gate means silence never
leaves the machine: it activates on speech, replays a one-second pre-roll so the
first word survives, streams continuously through the pauses in a sentence, and
releases after `stream.inactive_after_sec` (default 30) of quiet. On a sparse
day that is roughly 15% of what the microphone hears.

Transcripts land in `~/.cache/vani/transcript.jsonl` and expire after
`stream.retain_days`. Audio is never written to disk. No agent and no actions
yet — phase 1 exists to gather a real corpus before anything is tuned against it.

## Configuration

`~/.config/vani/config.toml` (mode 600 — it holds the token). `vani config show`
prints the effective values with the token masked.

| Key | Default | Meaning |
|---|---|---|
| `server.url` | `https://ai.lsquarelabs.com` | transcription API base URL |
| `server.token` | — | Bearer token; or `server.token_file`, or `$VANI_TOKEN` |
| `server.timeout_sec` | `120` | how long to wait for a transcript |
| `wake.enabled` | `true` | set false for hotkey-only (no vosk needed) |
| `wake.phrases` | `["hey claude", "hi claude"]` | words must exist in the Vosk vocabulary |
| `wake.model_dir` | `~/.local/share/vani/vosk-model-small-en-us-0.15` | model location |
| `recording.silence_sec` | `3` | silence that ends a recording |
| `recording.silence_warn_sec` | `1` | silence before the countdown appears |
| `recording.max_sec` | `120` | hard limit on one recording |
| `recording.auto_gain` | `true` | amplify quiet input before sending |
| `recording.speech_factor` | `3.5` | how far above the noise floor counts as speech |
| `recording.min_speech_level` | `350` | absolute bar for speech; lower for a quiet mic |
| `hotkey.enabled` / `.keycode` | `true` / `171` | the watched key |
| `output.typer` | `auto` | `xdotool`, `ydotool`, `clipboard`, `stdout` |
| `output.notify` / `.history` | `true` | desktop notifications / transcript log |

Unknown or mistyped keys are rejected at startup rather than silently ignored.

Find your key's keycode with:

```sh
xinput test-xi2 --root | grep -A2 RawKeyPress
```

## Files

```
~/.config/vani/config.toml     configuration (600)
~/.local/share/vani/           wake-word model
~/.cache/vani/history.log      transcripts
~/.cache/vani/last.wav         last clip sent, for debugging
$XDG_RUNTIME_DIR/vani/         status file and pidfiles
```

## Troubleshooting

Start with `vani doctor` — most failures here are environmental and it names
them directly. Then:

- **"(no speech detected)"** — listen to `~/.cache/vani/last.wav`: it is exactly
  what was sent. A wired or built-in mic (Settings → Sound → Input) beats a
  Bluetooth headset for ASR by a wide margin.
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

The GPU server is `~/sudhir/voice_api/voxtral_api.py`, run on dgx2 inside a
Slurm GPU-holder job:

```sh
cd ~/sudhir/voice_api && bash ~/use_instructions/run_in_holder.sh <HOLDER_JOBID> 0 \
    ~/sudhir/voice_api/voxtral_api.log \
    env LD_LIBRARY_PATH=/ihub/apps/python/3.10/lib .venv/bin/python voxtral_api.py --port 8000
```

`LD_LIBRARY_PATH` is required — the venv python can't find libpython3.10 without it.

Internet path: `dgx2:8000` → `ssh_bridge.sh` reverse-forward (login node) → VPS
`untu_vps` → cloudflared named tunnel `voice-api` → `ai.lsquarelabs.com`.
cloudflared cannot run on the cluster itself (edge port 7844 is blocked).

Endpoints: `GET /healthz` (no auth), `POST /transcribe` (raw WAV), and
`POST /v1/audio/transcriptions` (OpenAI-style multipart). The last two need
`Authorization: Bearer <token>`; the token lives at
`~/sudhir/voice_api/api_token.txt`.

## License

MIT — see [LICENSE](LICENSE).
