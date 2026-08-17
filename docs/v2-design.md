# vani v2 — ambient agent

Status: design, not built. v1 (dictation) keeps working throughout and stays
useful; v2 is a sibling daemon in this repo, not a fork and not a rewrite.

## What actually changes

v1 is an **event** pipeline: something triggers, one clip is captured, one
transcript comes back, one keystroke stream goes out. Every stage has a
beginning and an end, and the whole thing is stateless.

v2 is a **continuous** pipeline: audio never stops, the transcript is a rolling
stream, and the interesting logic is a decision loop over that stream whose
most common correct answer is *do nothing*. That inversion is the whole design
problem — the system has to reach "ignore" cheaply and reach it most of the
time.

## Shape

```
mic (on/off, user-controlled)
  │
  ├─ WebSocket ....................... wss://ai-stream.lsquarelabs.com/v1/realtime
  │    └─ 200 ms PCM16 frames up, transcription.delta words down
  │
  ├─ rolling transcript .............. local, timestamped, auto-expiring
  │
  ├─ agent ........................... a persistent Claude Code session
  │    └─ decides: ignore / inform / act
  │
  └─ executor ........................ tiered permissions + audit log
```

**The ASR is genuinely streaming**, which removes a whole layer that an earlier
draft of this document assumed. `mistralai/Voxtral-Mini-4B-Realtime-2602` runs
on vLLM behind an OpenAI-Realtime-style WebSocket: send `input_audio_buffer.append`
frames, receive `transcription.delta` events word by word. Measured end to end
from the desktop through the Cloudflare tunnel, on a 6.7 s clip: connect 0.6 s,
first delta 0.7 s, and words landing **0.3–0.5 s behind the speech**.

So there is no windowing, no energy gate, and no decision about where one
request ends and the next begins — the socket is the session. The batch
`/transcribe` endpoint on `ai.lsquarelabs.com` stays exactly where it is,
serving v1 dictation, untouched.

## Start / stop

One explicit control, and it is the privacy model as well as the UX:

- **Off** — the mic is closed and the socket is closed. Nothing is captured,
  nothing is sent, nothing reaches the agent. Not a filter, not a mute flag
  checked downstream: no audio leaves the machine because none is read.
- **On** — the socket is open and the transcript is live.

The equivalent framing is that "off" means *no prompt is sent to Claude at all*
— not a prompt saying to ignore things. State must be obvious at a glance from
the tray, because a listening indicator nobody trusts is worse than none.

## The agent

`pip install claude-agent-sdk` — Claude Code as a library, running in our own
process. Four things we need, four things it already has:

| What v2 needs | Agent SDK mechanism |
|---|---|
| A brain that persists across the day | `ClaudeSDKClient` — one session, many exchanges |
| Feed it utterances as they arrive | streaming input: `client.query(<async generator>)` |
| Inform freely, confirm before acting | `can_use_tool` callback + `allowed_tools` |
| Low-level actions (mic volume, notify) | `@tool` + `create_sdk_mcp_server` |
| Survive restarts, bound context | `resume` / `fork_session`, `list_sessions` |

The fit worth noticing is the third row. `can_use_tool` is an **async** callback
that returns `PermissionResultAllow` or `PermissionResultDeny` — so the
confirmation tier is not machinery we build, it is a callback that posts a
desktop notification and awaits the answer before returning. The same callback
is the natural place to write the audit log, because every tool call in the
system passes through it exactly once.

Auth: the daemon needs an `ANTHROPIC_API_KEY`. An always-on agent is a metered
thing even when cost is not the constraint.

## Actions

Three tiers, decided by the permission callback rather than by the prompt — a
prompt can be talked out of a rule, a callback cannot.

| Tier | Examples | Behaviour |
|---|---|---|
| **Inform** | notify, speak, summarize, answer, read transcript | auto-approved via `allowed_tools`; annotate `readOnlyHint=True` |
| **Act** | mic volume, settings, launching apps, writing files | confirmation prompt, then allow/deny |
| **Never** | anything not on either list | denied by default |

Default-deny is the point: a new tool is unusable until someone classifies it.
`permission_mode="default"` with an explicit `allowed_tools` set, never
`bypassPermissions`.

Every decision — tier, tool, input, verdict, and which utterance triggered it —
appends to an audit log. Without that, "why did it do that?" is unanswerable,
and it is the question this system will generate most.

## Retention

Transcript is local, timestamped, and auto-expires after N days. Audio is
never written to disk at all — frames go straight to the socket and are gone.
Stopping is the hard mute described above: capture ends at the source.

Note this is a real change from v1's privacy property. v1 advertises that audio
only leaves the machine once a recording has started; in v2 everything heard
while the mic is on is streamed. That is a deliberate choice — the start/stop
control above is what bounds it — and the README must say so plainly rather
than carrying v1's claim forward.

## What will actually be hard

1. **Addressivity.** Deciding whether speech was meant for the agent is the
   whole problem; everything else is plumbing. It cannot be tuned by reasoning
   about it, only against a corpus of a real room — which is why phase 1 exists.
2. **Self-hearing.** The moment the agent speaks, or a video plays, the mic
   hears it and feeds it back. Needs output-state gating. Easy to forget, and
   it produces the most confusing possible bug report.
3. **Context.** 24 h of utterances will not fit in a window. The daemon owns the
   transcript and hands the agent a bounded slice; sessions rotate on a
   schedule, seeded with a summary of the one before.
4. **Action safety.** An LLM triggering real work from speech it *inferred* was
   addressed to it. The permission tiers above are the answer, and they have to
   exist before the first side-effecting tool does.

## Phases

**1 — the ear.** Capture, the realtime WebSocket, rolling transcript, start/stop,
`vani listen` to tail it live. No agent, no actions.

Ship this first and run it for days, because it produces the one thing that
cannot be obtained any other way: **real numbers about this specific room.**
How many hours of speech a day. How many utterances were actually addressed to
an agent. What Voxtral's quality looks like on a Bluetooth HFP headset across a
whole day rather than one clip. How the socket behaves over hours — drops,
reconnects, drift. Every decision in phase 2 depends on those numbers, and
right now nobody has them.

**2 — the brain.** `ClaudeSDKClient` fed from the transcript. Inform-tier tools
only. Develop addressivity by replaying phase 1's log — no mic needed, the same
trick that makes `session.py` testable today.

**3 — the hands.** Custom tools, the `can_use_tool` confirmation tier, audit log.

**4 — hardening.** Self-hearing, session rotation, retention enforcement, mute.

## Reuse

Kept as-is: `audio.py` (capture), `config.py`, `paths.py`, `state.py`,
`output.py`, `notify.py`, `doctor.py`, and the install/systemd scaffolding.
`client.py` stays for v1's batch endpoint; v2 needs a new WebSocket client
beside it.

Replaced: `session.py`. Its wake/record/silence machine is precisely the
event-shaped assumption v2 drops. The new state machine should inherit its best
property — that it knows nothing about microphones, HTTP, or the desktop, and
is therefore testable from a WAV file with no network.

Unchanged: everything v1 does. Dictation remains the thing that works when the
agent is off, and `vani toggle` should keep working whatever v2 is doing.

## Running the ASR servers

Both live in the same Slurm holder on dgx2, on separate GPUs, and both are
published by the cloudflared tunnel on the VPS:

| hostname | port | model | used by |
|---|---|---|---|
| `ai.lsquarelabs.com` | 8000 | Voxtral batch `/transcribe` | v1 dictation |
| `ai-stream.lsquarelabs.com` | 8001 | Voxtral-Mini-4B-Realtime, vLLM | v2 |

The realtime server needs two things that are easy to get wrong, and cost an
afternoon to rediscover:

```sh
# on svs_ald (the ihub login node), inside the holder, on a free GPU
cd ~/sudhir/voice_api
bash ~/use_instructions/run_in_holder.sh <HOLDER_JOBID> 1 ~/sudhir/voice_api/vllm_realtime.log \
  env LD_LIBRARY_PATH=$PWD/cuda-compat/usr/local/cuda-13.0/compat:/ihub/apps/python/3.10/lib \
      HF_HOME=$PWD/hf_cache \
  .venv_vllm/bin/vllm serve mistralai/Voxtral-Mini-4B-Realtime-2602 \
      --host 0.0.0.0 --port 8001 --enforce-eager
```

1. **`cuda-compat` must be first on `LD_LIBRARY_PATH`.** dgx2's driver reports
   CUDA 12040, which torch rejects as too old; the compat `libcuda.so` in that
   directory is the fix. Without it: *"The NVIDIA driver on your system is too
   old"*. `/ihub/apps/python/3.10/lib` is still needed after it, for
   `libpython3.10.so.1.0`.
2. **The three flashinfer packages must be the same version.** vLLM 0.27.1
   hard-imports flashinfer, `flashinfer-cubin` publishes nothing above 0.6.13,
   and `flashinfer-jit-cache` only exists as `+cu130` on the flashinfer index —
   so the working set is `flashinfer-python`, `flashinfer-cubin`, and
   `flashinfer-jit-cache` all at 0.6.13, the last from
   `--extra-index-url https://flashinfer.ai/whl/cu130`.

**Always install into `.venv_vllm` with `--no-deps`.** `flashinfer-python`
declares an older torch, so a plain install silently drags torch 2.13 → 2.9 and
clobbers cu13 NCCL with a cu12 build, which then fails as
`undefined symbol: ncclCommResume`. `--no-deps` makes that impossible.
