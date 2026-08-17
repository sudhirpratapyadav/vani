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
mic (never stops)
  │
  ├─ windowing + energy gate ......... local, no ASR
  │    └─ fixed windows, silent ones dropped before they leave the machine
  │
  ├─ Voxtral /transcribe ............. every non-silent window
  │    └─ the existing v1 client, unchanged
  │
  ├─ rolling transcript .............. local, timestamped, auto-expiring
  │
  ├─ agent ........................... a persistent Claude Code session
  │    └─ decides: ignore / inform / act
  │
  └─ executor ........................ tiered permissions + audit log
```

The energy gate is **not** speech recognition and not vosk — it is the RMS and
adaptive-noise-floor maths already in `audio.py` and `session.py`, deciding
only "is anything happening". It exists for transcript quality, not to save
money: ASR models hallucinate confidently on silence, and a transcript salted
with invented sentences poisons every decision made downstream of it.

Windowing is unavoidable regardless: Voxtral is a batch model, so something has
to decide where one request ends and the next begins. There is no streaming
mode to fall back on — "live text" here means utterance latency, ~1–3 s.

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
discarded the moment its window has been transcribed — nothing is ever written
to disk. A hard mute must stop capture at the source, not just filter
downstream, and it must be obvious from the tray whether the mic is live.

Note this is a real change from v1's privacy property. v1 advertises that audio
only leaves the machine once a recording has started; in v2 every non-silent
window is sent. That is a deliberate choice, and the README must say so plainly
rather than carrying v1's claim forward.

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

**1 — the ear.** Capture, windowing, energy gate, Voxtral, rolling transcript,
`vani listen` to tail it. No agent, no actions.

Ship this first and run it for days, because it produces the one thing that
cannot be obtained any other way: **real numbers about this specific room.**
How many hours of speech a day. How many utterances were actually addressed to
an agent. What Voxtral's quality looks like on a Bluetooth HFP headset across a
whole day rather than one clip. What fraction of windows are silence. Every
decision in phase 2 depends on those numbers, and right now nobody has them.

**2 — the brain.** `ClaudeSDKClient` fed from the transcript. Inform-tier tools
only. Develop addressivity by replaying phase 1's log — no mic needed, the same
trick that makes `session.py` testable today.

**3 — the hands.** Custom tools, the `can_use_tool` confirmation tier, audit log.

**4 — hardening.** Self-hearing, session rotation, retention enforcement, mute.

## Reuse

Kept as-is: `audio.py`, `client.py`, `config.py`, `paths.py`, `state.py`,
`output.py`, `notify.py`, `doctor.py`, and the install/systemd scaffolding.

Replaced: `session.py`. Its wake/record/silence machine is precisely the
event-shaped assumption v2 drops. The new state machine should inherit its best
property — that it knows nothing about microphones, HTTP, or the desktop, and
is therefore testable from a WAV file with no network.

Unchanged: everything v1 does. Dictation remains the thing that works when the
agent is off, and `vani toggle` should keep working whatever v2 is doing.
