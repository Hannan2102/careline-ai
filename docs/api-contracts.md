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
| `GET` | `/api/patients/{id}` | Synthetic patient detail |
| `GET` | `/api/patients?query=` | Dashboard search |

## Appointments

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/appointments` | Filter by patient, provider, date range, status |
| `GET` | `/api/appointments/slots` | Availability search |
| `POST` | `/api/appointments` | Book |
| `POST` | `/api/appointments/{id}/cancel` | Cancel |
| `POST` | `/api/appointments/{id}/reschedule` | Reschedule |

## Medications

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/medications/{patient_id}` | Active medications |
| `POST` | `/api/medications/refill-requests` | Create a refill request (pending review) |
| `GET` | `/api/medications/refill-requests` | Dashboard list |

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
| `GET` | `/api/calls/{id}/trace` | Per-turn trace for the dashboard |
| `GET` | `/api/escalations` | Filter by category, priority, status |
| `POST` | `/api/escalations/{id}/resolve` | Mark handled (staff action) |
| `GET` | `/api/providers` | Practitioners and specialties |
| `GET` | `/api/usage/summary` | Spend today, project total, remaining budget, guard status |

## Conventions

Errors use a consistent envelope:

```json
{"error": {"code": "NOT_VERIFIED", "message": "Session is not verified.",
           "details": {}, "trace_id": "..."}}
```

Codes mirror the tool error taxonomy in [agent-tools.md](agent-tools.md). Timestamps are
ISO-8601 UTC. Dates are `YYYY-MM-DD`. Patient references are FHIR ids. Every response
carries `X-Trace-Id`, correlating with the structured logs.

Auth is out of scope pre-Phase 10: the dashboard is local-only. A real deployment needs
authenticated, role-based staff access — noted in SAFETY.md as a gap, not glossed over.
