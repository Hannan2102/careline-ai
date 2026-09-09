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
