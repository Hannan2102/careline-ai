# System design

A narrower, more operational companion to [../ARCHITECTURE.md](../ARCHITECTURE.md).

## Processes

| Process | Runs | Purpose |
|---|---|---|
| `backend` | FastAPI / uvicorn | REST API, agent runtime, services |
| `voice-agent` | worker (Phase 13) | Joins LiveKit rooms, drives the audio pipeline |
| `frontend` | Next.js | Admin dashboard |
| `hapi-fhir` | Docker | FHIR R4 server |
| `postgres` | Docker | HAPI storage + application schema |

The voice agent is a separate process from the API on purpose: audio work must not compete
with request handling, and a crash in the audio path must not take the API down.

## Application schema

Owned by us; contains no clinical assertions.

| Table | Purpose |
|---|---|
| `session` | One conversation. Channel, verification state, patient ref, timestamps |
| `turn` | One exchange. Transcript, intent, workflow, safety decision, latencies, cost |
| `tool_call` | Arguments, result, duration, validation outcome |
| `audit_event` | Append-only record of every access and mutation |
| `escalation` | Structured handoff: category, priority, destination, summary, transcript |
| `refill_request` | Pending clinician review; references a FHIR MedicationRequest |
| `provider_usage` | Metered units and estimated cost per provider |

Patients are referenced by FHIR id only. Demographics are never copied here.

## Configuration

One `Settings` object (pydantic-settings), loaded once, injected. No module reads
`os.environ` directly. Invalid combinations fail fast at startup — for example
`TEXT_ONLY_MODE=true` with `VOICE_ENABLED=true` is resolved deterministically in favour of
text-only, and a paid provider selected without an API key is rejected before the first
request rather than at the first call.

## Failure behaviour

| Failure | Behaviour |
|---|---|
| FHIR server unreachable | Tool returns a typed error; agent apologises and escalates. No invented data |
| LLM provider error | Bounded retry, then escalate to a human |
| Tool validation failure | Repair prompt, bounded retries, then ask the user or escalate |
| Budget ceiling reached | Optional paid calls blocked; mock/local substituted; text mode continues |
| STT/TTS failure mid-call | Fall back to a scripted apology and transfer |

The recurring principle: **degrade to a human, never to a guess.**

## Concurrency

Slot booking is the one genuinely contended operation. The service re-reads slot status
inside the write path and fails the loser of a race with a typed conflict, which the
workflow turns into "that time was just taken — here are two others." Correctness is
enforced at the data layer, not by conversational politeness.
