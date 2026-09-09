# API contracts

FastAPI, OpenAPI at `/docs`. Everything is versionless under `/api` while pre-1.0.

## System

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness. Cheap, no dependencies. |
| `GET` | `/api/system/status` | Configuration and dependency health: EHR provider and reachability, AI mode, per-kind providers, mode switches, budget status |

`/health` deliberately touches nothing external, so it stays a true liveness signal.
`/api/system/status` is the diagnostic view and may report `degraded` without failing.

## Patients

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/patients/{id}` | Synthetic patient detail: demographics, medications, appointments, conditions, allergies |
| `GET` | `/api/patients?query=&limit=` | Dashboard roster search (substring of the full name) |

Both are **staff-facing** reads and are not scoped to a verified session. Nothing in the
agent runtime may reach them: a caller-facing path that can list the roster defeats
verification entirely (ADR 003). Dosage text is passed through exactly as the record
stores it, and is `null` when the record has none — never assembled (SAFETY.md).

## Appointments

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/appointments` | Clinic schedule: `start_date`, `end_date`, `practitioner_ref`, `include_cancelled` |
| `GET` | `/api/appointments/slots` | Availability search |
| `POST` | `/api/appointments` | Book |
| `POST` | `/api/appointments/{id}/cancel` | Cancel |
| `POST` | `/api/appointments/{id}/reschedule` | Reschedule |

Only the `GET` is implemented. Booking, cancelling, and rescheduling happen through the
agent (`POST /api/agent/.../turns`), which reaches the same services with verification
and ownership enforced; exposing them again as unauthenticated HTTP would be a second,
weaker door onto the same records.

## Medications

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/medications/{patient_id}` | Active medications |
| `POST` | `/api/medications/refill-requests` | Create a refill request (pending review) |
| `GET` | `/api/medications/refill-requests` | Dashboard list, filterable by `status` |

Implemented: the refill-request list, and medications as part of `/api/patients/{id}`.
Creating a refill request is an agent action for the same reason as booking above.

## Agent

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/agent/sessions` | Start a session |
| `POST` | `/api/agent/sessions/{id}/turns` | Submit an utterance, receive a response + trace |
| `GET` | `/api/agent/sessions/{id}` | Session state + full trace |

`POST /turns` is the single entry point to the runtime and is what both the CLI and the
voice agent call. One entry point is what keeps text and voice honest.

## Calls, escalations, providers

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/calls` | Session list with outcome, duration, cost |
| `GET` | `/api/calls/{id}/trace` | Per-turn trace, with each turn's audited record operations |
| `GET` | `/api/escalations` | Filter by `category` and `priority` |
| `GET` | `/api/providers` | Practitioners and specialties |
| `GET` | `/api/clinic` | The fictional clinic's details, and a disclaimer saying so |
| `GET` | `/api/overview` | Rolling 24-hour counts, latency, intents, record operations, spend |
| `GET` | `/api/usage/summary` | Spend today, project total, remaining budget, guard status |

Everything the dashboard reads is **GET**. `POST /api/escalations/{id}/resolve` and any
other staff action is deliberately absent until there is staff authentication to hang it
on — see the note below and SAFETY.md.

The call, trace, escalation, refill, and usage reads all come from the persisted tables
rather than the in-process stores, so a call stays inspectable after the process that
handled it has gone. With `PERSISTENCE_ENABLED=false` they return **503** with an
explanation rather than an empty list, because an empty dashboard that is empty for the
wrong reason looks exactly like a quiet clinic.

## Conventions

Errors use a consistent envelope:

```json
{"error": {"code": "NOT_VERIFIED", "message": "Session is not verified.",
           "details": {}, "trace_id": "..."}}
```

Codes mirror the tool error taxonomy in [agent-tools.md](agent-tools.md). Timestamps are
ISO-8601 UTC. Dates are `YYYY-MM-DD`. Patient references are FHIR ids. Every response
carries `X-Trace-Id`, correlating with the structured logs.

Auth is still out of scope: the dashboard is local-only and every record in it is
invented. A real deployment needs authenticated, role-based staff access before any of
these endpoints are exposed — noted in SAFETY.md as a gap, not glossed over.

CORS is configured, never wildcarded: `DASHBOARD_ORIGINS` names the browser origins the
API will answer, defaulting to the dashboard's development ports.
