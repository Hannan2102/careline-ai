# Frontend — admin dashboard

Next.js (App Router) + TypeScript strict + Tailwind, talking to the FastAPI backend.
This is the staff-facing view of what the agent did: **CareLine AI's operations console**,
not a patient-facing product.

```bash
npm install
npm run dev        # http://localhost:3000
```

It needs the backend running (`make dev` in the repository root). The API base URL comes
from `NEXT_PUBLIC_API_BASE_URL`, defaulting to `http://localhost:8000`; copy
`.env.example` to `.env.local` to change it.

| Page | Shows |
|---|---|
| **Overview** | Calls, turns, escalations and average latency over the last 24 hours; turns by intent; record operations counted from the audit trail; spend today, spend project-to-date, remaining budget, and the guard's status |
| **Calls** | Every conversation: patient, verification state, last intent, workflow, turns, duration, estimated cost, and derived outcome |
| **Agent Trace** | Per turn: transcript, safety decision and the rule that fired, intent and confidence, extracted entities, workflow and its state, every audit-logged record operation, per-stage latency, and estimated cost |
| **Patients** | The synthetic roster and each chart the agent can read — demographics, medications with verbatim dosage text, appointments, conditions, allergies |
| **Appointments** | The clinic schedule by day, filterable by provider and date range |
| **Escalations** | Every handoff with its structured summary, plus the refill queue awaiting clinician review |

## Two decisions worth knowing

**Data is fetched in the browser, not on the server.** Every page is a client component
calling the API through `useApi`. That keeps `next build` hermetic — CI builds the
dashboard with no backend and no network — and it makes a backend that is down render as
a visible error instead of a broken page. The dashboard is a live operations view of a
local service; server rendering would buy caching it does not want.

**The synthetic-data banner is part of the layout, not a page.** It is fixed to the top of
every route with no way to dismiss it. A screenshot of this dashboard has to be
unmistakable at a glance, and a footnote would not survive being cropped.

## Read-only

The dashboard reads. There is no "approve this refill" button and no "resolve this
escalation" button, because there is no authentication to hang a staff action on. Those
endpoints arrive with staff auth, not before — see [../SAFETY.md](../SAFETY.md).

## Checks

```bash
npm run lint       # eslint, eslint-config-next
npm run typecheck  # tsc --noEmit, strict + noUncheckedIndexedAccess
npm run build      # next build
```

All three run in CI.
