# Latency

**Target: 0.8–2.0 s perceived**, measured from the caller finishing speaking to the first
audio of the response.

## Budget

| Stage | Target | Notes |
|---|---|---|
| STT finalisation | 150–400 ms | Endpointing dominates; streaming is mandatory |
| LLM decision / first useful output | 200–800 ms | Short prompts, capped output, streaming |
| Tool + FHIR round trip | 20–200 ms | Local HAPI; a slow query here is a bug |
| TTS first audio | 75–300 ms | Streaming synthesis, first sentence only |
| **Perceived total** | **0.8–2.0 s** | |

A turn with a tool call spends two LLM round trips. Where the result is deterministic (a
confirmation, a slot list), the response is templated rather than generated — cheaper and
faster than a second model call.

## Instrumentation

Recorded per turn: `stt_final_ms`, `intent_ms`, `tool_ms` (per call), `fhir_ms`,
`llm_first_token_ms`, `llm_total_ms`, `tts_first_audio_ms`, `turn_total_ms`. Emitted in
structured logs and rendered in the dashboard's Agent Trace.

## Measured

**2026-09-11**, from 148 recorded voice turns and a live A/B of the speech provider.

| Stage | Budget | Measured | |
|---|---|---|---|
| STT recognition lag | 150–400 ms | **18 ms** median | inside |
| Safety + extraction + workflow | 220–1000 ms | **5 ms** median, 475 ms p90 | inside |
| TTS first audio *(before)* | 75–300 ms | **375 ms** median | over |
| TTS first audio *(after)* | 75–300 ms | **130 ms** median | inside |
| Perceived turn | 0.8–2.0 s | **0.38 s** median | inside |

STT "recognition lag" is time from the last audio frame to a final transcript, and
excludes the 1.8 s end-of-utterance wait — that is a policy choice in `TurnTimings`, not
something Deepgram did (and the wait exists because 800 ms cut callers off mid-date-of-
birth).

### The one optimisation: speech over a held-open socket

Synthesis was the only stage outside its budget, and at 375 ms it was most of the
perceived turn. Three paths, **interleaved** in one session so the same network answers
every question, with seven seconds between utterances as a real call has:

| Path | First utterance | Then median |
|---|---|---|
| REST, default 5 s keepalive | 523 ms | **445 ms** |
| REST, 120 s keepalive | 418 ms | **335 ms** |
| Websocket, held open for the call | 377 ms | **130 ms** |

`make measure-tts ARGS="--ab --gap 7"` reproduces it.

**The gap is the measurement.** Run back to back with no pause, all three paths come out
identical at ~125 ms, and the first version of this benchmark therefore showed no
improvement at all. A caller speaks for several seconds between replies and httpx expires
an idle connection after five, so almost every reply was paying for a TLS handshake
before a single sample was synthesised. A benchmark that fires requests in a tight loop
measures a warm pool no real call ever has.

Two findings worth keeping:

- **It is the connection, not the streaming.** Raising the keepalive alone recovers about
  110 ms of the 315 — real, and a great deal cheaper than a websocket — but it cannot
  hold a connection the server is entitled to close. The socket is opened once and kept.
- **Opening a socket per utterance is slower than REST.** Measured that way first, and it
  lost: a handshake plus an upgrade costs more than streaming saves. The optimisation is
  the *held* connection; the streaming is what makes long replies no slower than short
  ones.

Cost of the change: a socket has more ways to fail than a POST, so a failure before the
first byte falls back to REST, and an interrupted utterance discards the connection
rather than letting the next reply inherit its queued audio.

## Method

Measure before optimising. Every optimisation in this project must cite a before/after
number from real recorded turns — not an intuition about which stage "feels" slow.

Candidate levers, in the order they usually pay off: shrink the system prompt; summarise
rather than replay history; template deterministic responses; start TTS at the first
sentence boundary; prefetch likely slot queries while the caller states a reason; keep HTTP
connections warm to all three providers; and only then consider a smaller model.

Perceived latency also responds to things that are not latency: a short acknowledgement
("let me check that") makes a 1.5 s gap feel unremarkable. Used sparingly, and never as a
substitute for actually being fast.
