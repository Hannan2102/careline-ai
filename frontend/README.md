# Frontend — admin dashboard

**Not built yet.** This lands in **Phase 10** ([../ROADMAP.md](../ROADMAP.md)).

The directory is a placeholder rather than a scaffold on purpose: an empty Next.js app
that renders nothing would be dead code in the repository and noise in the git history.

## Planned

Next.js (App Router) + TypeScript strict + Tailwind, talking to the FastAPI backend at
`NEXT_PUBLIC_API_BASE_URL`.

| Page | Shows |
|---|---|
| **Overview** | Calls today, appointments booked/changed, FAQs resolved, refill requests, escalations, average latency, resolution rate, estimated spend today and project-to-date, remaining budget |
| **Calls** | Session id, synthetic patient, verification state, intent, transcript, workflow, outcome, duration, escalation state, estimated session cost |
| **Agent Trace** | Per turn: transcript, detected intent, extracted entities, workflow, safety decision, each tool call with arguments and result, LLM response, and per-stage latency (STT, LLM, FHIR, TTS first audio, total) plus estimated turn cost |
| **Patients** | Synthetic patient search — demographics, medications, appointments, conditions. Labelled unmistakably as synthetic |
| **Appointments** | Calendar and list by provider, patient, type, status, duration |
| **Escalations** | Clinical, failed-verification, patient-requested, system-uncertainty, administrative — with the structured handoff summaries |

Agent Trace is the page that matters: it exists to make the system debuggable, and it
demos well as a consequence — not the other way round.
