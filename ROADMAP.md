# Roadmap

18 phases. Text mode and deterministic workflows come **before** any paid voice
integration — that ordering is a budget control as much as an engineering one.

Legend: ✅ done · 🚧 in progress · ⬜ not started

---

### ✅ Phase 0 — Architecture and repo setup
Repo, structure, documentation, ADRs, env contract, tooling.

**Acceptance**
- [x] Git repo initialised, `.gitignore` excludes secrets/artifacts
- [x] README, ARCHITECTURE, ROADMAP, SAFETY, FHIR, AGENTS, DEMO, COSTS, PROJECT_STATUS present
- [x] ADRs 001–006 written
- [x] Monorepo folder structure with clear layer separation
- [x] `.env.example` covers every configuration switch, defaults to $0 text mode

---

### ✅ Phase 1 — Local EHR foundation
HAPI FHIR + PostgreSQL via Docker Compose, plus a no-Docker in-memory provider so the
foundation is usable and testable on any machine.

**Acceptance**
- [x] `docker-compose.yml` defines `postgres` and `hapi-fhir`
- [x] `EHRProvider` interface defined; `MemoryFHIRProvider` implements it fully
- [x] `LocalFHIRProvider` implemented against the FHIR REST API
- [x] FHIR R4 client with typed resource ↔ domain mappings
- [x] Curated clinic, providers, and demo patients seedable into either provider
- [x] FastAPI app exposes `GET /health` and `GET /api/system/status`
- [x] Test suite green with no network access and no cost
- [x] Verified against a running HAPI server: `/fhir/metadata` responds (FHIR 4.0.1),
      984 resources seed, patient retrieval round-trips, booking/cancellation persist,
      and the server refuses a double booking

---

### ⬜ Phase 2 — FHIR service layer
Domain services over the EHR interface: patients, scheduling, medications.

**Acceptance**
- Services expose domain models only; no FHIR types escape the adapter
- Slot/appointment/medication reads and writes covered by integration tests against both
  providers, with identical assertions

---

### ⬜ Phase 3 — Patient verification
Session-scoped identity verification: name + DOB, secondary factor on ambiguity.

**Acceptance**
- Exactly one match → `VERIFIED`; zero matches → no information disclosed; multiple
  matches → second factor requested, never disambiguated by leaking a record
- Verification state cannot be set by conversation text, only by the verification service
- Every PHI-shaped tool refuses when the session is unverified
- Failed verification after N attempts creates an escalation

---

### ⬜ Phase 4 — Appointment scheduling
Slot search, appointment-type classification, provider selection, booking.

**Acceptance**
- Type determines duration; slot must exist, be free, and fit the duration
- Provider must be working; no double-booking; no conflicting patient appointment
- Concurrent booking of one slot: exactly one succeeds
- Booking mutates EHR state and is visible on re-read

---

### ⬜ Phase 5 — Appointment management
Lookup, cancel, reschedule.

**Acceptance**
- Cancellation releases the slot; reschedule releases old and books new atomically
- All operations require verification and are audited

---

### ⬜ Phase 6 — Medication retrieval
Active `MedicationRequest` lookup with verbatim dosage text.

**Acceptance**
- Returned dosage string is byte-identical to the stored instruction
- Response attributes the value to the prescription on file
- No path allows the LLM to author or alter a dosage

---

### ⬜ Phase 7 — Refill requests
Create a refill request pending clinician review.

**Acceptance**
- Persists a `refill_request` in `PENDING_REVIEW`, linked to an active MedicationRequest
- Never authorises, never creates a prescription, never changes a dose
- Patient is told it was sent for review, not that it was approved

---

### ⬜ Phase 8 — Safety and escalation
Deterministic policies and structured handoffs.

**Acceptance**
- Every category in SAFETY.md has a policy, a unit test, and a fixed escalation route
- Refusals carry no medical content
- Handoffs capture patient, verification state, medication, concern, verbatim question,
  the fact that no advice was given, destination, and priority
- Policies evaluate before workflow dispatch and are not promptable

---

### ⬜ Phase 9 — Text agent interface
CLI + dev chat endpoint over the full runtime.

**Acceptance**
- All Phase 3–8 workflows completable in text with a mock LLM (deterministic, $0)
- Same workflow objects as voice; no parallel implementation
- Transcript, intent, tool calls, and safety decisions recorded per turn

---

### ⬜ Phase 10 — Admin dashboard
Next.js: Overview, Calls, Agent Trace, Patients, Appointments, Escalations.

**Acceptance**
- Agent Trace renders every field of a turn record including latency and cost
- Synthetic-data labelling is unmissable
- TypeScript strict, lint and build clean

---

### ⬜ Phase 11 — Usage tracking and budget controls
`provider_usage` persistence, pricing, guard enforcement, dashboard surfacing.

**Acceptance**
- Every provider call records units and estimated cost
- Warn threshold surfaces a warning; max threshold blocks optional paid calls
- Guard behaviour is unit-tested at both thresholds
- Text/mock mode always remains available

---

### ⬜ Phase 12 — Cloud provider integrations
`OpenAILLMProvider`, `DeepgramSTTProvider`, `ElevenLabsTTSProvider`.

**Acceptance**
- Each implements its interface and reports usage
- Tool-call arguments validated; invalid output triggers repair/retry, never execution
- CI never invokes a paid API
- First live smoke test costs < $0.25 and is logged in COSTS.md

---

### ⬜ Phase 13 — LiveKit browser voice
Browser mic → Deepgram → agent → ElevenLabs → speaker.

**Acceptance**
- A full booking completes by voice in the browser
- Barge-in, silence, and timeout handled by the turn manager
- Voice changes no business logic
- Session cost cap enforced live

---

### ⬜ Phase 14 — Latency optimisation
Measure, then optimise.

**Acceptance**
- Per-stage latency recorded for every turn and visible in the dashboard
- Median perceived turn latency within 0.8–2.0 s on the demo scenarios
- Each optimisation references a before/after measurement

---

### ⬜ Phase 15 — Twilio phone integration *(opt-in, budget permitting)*
Twilio number → SIP → LiveKit → existing pipeline.

**Acceptance**
- A real call reaches the agent and completes a booking
- Zero duplicated business logic
- Explicit developer approval recorded before enabling

---

### ⬜ Phase 16 — Epic sandbox adapter
`EpicFHIRProvider` against Epic's developer sandbox, SMART on FHIR / OAuth.

**Acceptance**
- Implements `EHRProvider`; swapping `EHR_PROVIDER` changes no agent code
- Documented auth flow; sandbox-only, no production credentials

---

### ⬜ Phase 17 — Optional local AI mode
`WhisperSTTProvider`, `OllamaLLMProvider`, `PiperTTSProvider`.

**Acceptance**
- `AI_MODE=local` completes the text workflows end to end
- Tool-call reliability measured and documented honestly against cloud
- No paid call possible in this mode

---

### ⬜ Phase 18 — Testing and polished demo
Coverage, demo scripts, screenshots, recording, final docs pass.

**Acceptance**
- All nine workflow scenarios in DEMO.md pass as automated tests
- Demo reset script produces a reproducible environment every run
- Clean git history, no secrets, honest PROJECT_STATUS
