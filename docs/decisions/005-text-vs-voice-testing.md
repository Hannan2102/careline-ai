# ADR 005 — Text mode is the primary development and test surface

**Status:** Accepted · **Date:** 2026-09-08

## Context

Voice is the product's headline, and voice is metered twice per turn. The interesting
engineering — verification, scheduling constraints, safety, escalation — is entirely
independent of the audio path.

## Decision

Text mode is the default and shares the *same* orchestrator, workflows, tools, services,
and safety policies as voice. Voice adds STT before the runtime, TTS after it, and a turn
manager. No business logic is duplicated. All automated tests run in text mode with mock
providers. Voice is exercised only for speech-specific behaviour: recognition of names and
dates, turn-taking, barge-in, silence, latency, voice quality.

## Consequences

**Good** — The test suite is free, fast, deterministic, and CI-safe. ~90–95% of development
costs nothing. A workflow bug is reproducible without a microphone.

**Costs** — Voice-only defects (endpointing, barge-in, mis-recognised dates) are not caught
by the text suite and need explicit, scripted voice sessions.

**Enforcement** — if a behaviour differs between text and voice, that is a bug in the
layering, not a feature to be configured.
