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

- **The silence threshold adapts.** A fixed threshold works at a desk and fails
  on a train, so the noise floor is tracked as an exponential average while
  idle and speech is anything ~3.5× louder (with a hard floor).
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
~/.cache/vani/daemon.log       daemon output under systemd
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
  that needs `xdotool`.
- **Recordings cut off mid-sentence** — raise `recording.silence_sec`, or the
  room is loud enough that the adaptive threshold is treating speech as silence;
  `vani start` in a terminal logs the countdown decisions.
- **Logs** — `journalctl --user -u vani-daemon -f` or `~/.cache/vani/daemon.log`.

### Wayland

The media-key watcher is X11-only. Typing falls back to `ydotool` (which needs
its own daemon and `/dev/uinput` access) and then to the clipboard, so
transcripts survive even where they can't be typed. Wake words and push-to-talk
via a desktop shortcut work either way.

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
