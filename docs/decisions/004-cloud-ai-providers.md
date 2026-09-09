# ADR 004 — Discrete STT → LLM → TTS pipeline, cloud-first, vendor-neutral

**Status:** Accepted · **Date:** 2026-09-08

## Context

Two axes: (1) a single speech-to-speech realtime model versus a discrete pipeline, and
(2) which vendors to start with. The project must stay debuggable, testable, cheap, and
portable to fully local inference later.

## Decision

Use a discrete pipeline — streaming STT → text LLM with tool calling → TTS — behind three
provider interfaces. Start with Deepgram (Nova-3/Flux), OpenAI, and ElevenLabs
(Flash/Turbo) as adapters. Ship `Mock*` providers as first-class implementations.

## Consequences

**Good** — A text boundary exists at every stage, which is what makes tool-call validation,
safety enforcement, transcripts, per-stage latency and cost attribution, deterministic
tests, and provider swapping possible. Local providers (Phase 17) drop in without touching
application code. Zero-cost development is achievable.

**Costs** — More moving parts than one realtime model, and slightly higher floor latency
from three network hops. The 0.8–2.0 s target is still comfortably reachable with
streaming at each stage.

**Not precluded** — a realtime speech-to-speech model can be added later as a fourth
provider kind, and its behaviour compared against the pipeline on the same workflows.

## Alternatives

*Speech-to-speech only* — lowest latency, but collapses the seams the safety and testing
strategy depend on. Rejected as the primary architecture, not as an option.
*Local-first from day one* — good cost story, but weaker tool-calling reliability while
the workflows themselves are still being designed. Deferred to Phase 17.
