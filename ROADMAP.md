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

### ✅ Phase 2 — FHIR service layer
Domain services over the EHR interface: patients, scheduling, medications.

**Acceptance**
- [x] Services expose domain models only; no FHIR types escape the adapter
- [x] Slot/appointment/medication reads and writes covered by integration tests against both
      providers, with identical assertions — one contract suite, parametrised over
      `MemoryFHIRProvider` and live HAPI (64 + 64 assertions)
- [x] Appointment ownership enforced in the service layer, so no caller can change a
      stranger's appointment by guessing an id
- [x] Medication lookup distinguishes found / not-found / ambiguous / no-dosage-on-file
- [x] Verbatim-dosage guard rejects any rendering that alters the stored instruction

---

### ✅ Phase 3 — Patient verification
Session-scoped identity verification: name + DOB, secondary factor on ambiguity.

**Acceptance**
- [x] Exactly one match → `VERIFIED`; zero matches → no information disclosed; multiple
      matches → second factor requested, never disambiguated by leaking a record
- [x] A wrong date of birth is byte-identical in response to an unknown name
- [x] Verification state cannot be set by conversation text, only by the verification
      service — `patient_ref` and `verification` are read-only properties, and the only
      mutator requires a `VerificationDecision`
- [x] The PHI gate (`require_verified_patient`) refuses unverified sessions, and refuses a
      patient reference that does not match the session's own
- [x] Failed verification after 3 attempts locks the session and creates an escalation
- [ ] Every tool calls the gate — the tool layer lands in Phases 4–8; a contract test will
      enumerate the tools once they exist

---

### ✅ Phase 4 — Appointment scheduling
Slot search, appointment-type classification, provider selection, booking — as an explicit
multi-turn state machine (ADR 002).

**Acceptance**
- [x] Type determines duration; slot must exist, be free, and fit the duration
- [x] Provider must be working; no double-booking; no conflicting patient appointment
- [x] Concurrent booking of one slot: exactly one succeeds — proved at the workflow level
      as well as the EHR level, with the loser re-offered other times
- [x] Booking mutates EHR state and is visible on re-read
- [x] Booking is unreachable without verification, and the workflow has no field for a
      caller-supplied patient reference at all
- [x] A half-finished booking is JSON-serialisable and resumable

---

### ✅ Phase 5 — Appointment management
Lookup, cancel, reschedule — with disambiguation when the patient has more than one.

**Acceptance**
- [x] Cancellation releases the slot, verified by re-searching availability
- [x] Reschedule releases old and books new atomically; a lost race leaves the original
      appointment in place and says so
- [x] All operations require verification and are audited
- [x] Several appointments are disambiguated before acting, rather than guessing
- [x] The audit trail records what was touched, never the clinical content itself

---

### ✅ Phase 6 — Medication retrieval
Active `MedicationRequest` lookup with verbatim dosage text.

**Acceptance**
- [x] Returned dosage string is byte-identical to the stored instruction, asserted against
      the EHR rather than against a constant
- [x] Response attributes the value to the prescription on file
- [x] No path allows the LLM to author or alter a dosage: the answer is rendered from a
      template and re-checked before it is spoken
- [x] A prescription with no instruction text escalates rather than being reconstructed
- [x] Unknown medications are reported as absent, never approximated

---

### ✅ Phase 7 — Refill requests
Create a refill request pending clinician review.

**Acceptance**
- [x] Persists a `refill_request` in `PENDING_REVIEW`, linked to an active MedicationRequest
- [x] Never authorises, never creates a prescription, never changes a dose — `RefillService`
      has no approve/authorise/fulfil method at all, asserted by a test
- [x] The EHR is provably unchanged by a refill request
- [x] Patient is told it was sent for review, not that it was approved
- [x] No active prescription escalates instead of creating an orphan request
- [x] A duplicate request is not stacked on the clinician's queue

---

### ✅ Phase 8 — Safety and escalation
Deterministic policies and structured handoffs. Built ahead of Phases 4–7 so the safety
layer exists before there is an agent to constrain.

**Acceptance**
- [x] Every category in SAFETY.md has a policy, a unit test, and a fixed escalation route
      derived from the category rather than supplied
- [x] Each category also has a **negative** test: over-blocking "when is my appointment?"
      would make the product useless, so that is a failure mode too
- [x] Refusals carry no medical content, enforced by `contains_medical_instruction`
- [x] Handoffs capture patient, verification state, medication, concern, the question
      **verbatim**, the fact that no advice was given, destination, and priority
- [x] Policies are not promptable: six injection attempts are refused unchanged, and a
      model flag may only *add* a refusal, never remove one
- [ ] Policies evaluate before workflow dispatch — the classifier is built and ordered;
      wiring it ahead of dispatch happens with the orchestrator in Phase 9

---

### ✅ Phase 9 — Text agent interface
Orchestrator, CLI, and dev chat endpoint over the full runtime.

**Acceptance**
- [x] All Phase 3–8 workflows completable in text, deterministically and at $0
- [x] Safety runs first and unconditionally: workflows are reachable only from the ALLOW
      branch, so there is no path from an utterance to a record that skips it
- [x] Same workflow objects as voice; voice adds STT before `handle_turn` and TTS after
- [x] Transcript, intent, entities, workflow, safety decision, record operations,
      per-stage latency, and estimated cost recorded per turn
- [x] Six DEMO.md scenarios run as executable tests
- [x] The turn limit hands over to a human rather than looping (also a cost ceiling)

The extraction seam is deterministic for now: `RuleBasedExtractor` implements the
`TurnExtractor` protocol, and Phase 12 adds an LLM implementation of the same protocol.
Safety runs before extraction either way, so a missed extraction can only ever produce
"I didn't understand" — never an unsafe action.

---

### ✅ Phase 10 — Admin dashboard
Next.js: Overview, Calls, Agent Trace, Patients, Appointments, Escalations.

**Acceptance**
- Agent Trace renders every field of a turn record including latency and cost
- Synthetic-data labelling is unmissable
- TypeScript strict, lint and build clean

---

### ✅ Phase 11 — Persistence, usage tracking, and budget controls
The full application schema — `session`, `turn`, `audit_event`, `escalation`,
`refill_request`, `provider_usage` — plus the guard reading a spend total that survives
a restart.

**Acceptance**
- [x] Every provider call records units and estimated cost, persisted to `provider_usage`
- [x] Warn threshold surfaces a warning; max threshold blocks optional paid calls
- [x] Guard behaviour is unit-tested at both thresholds, including a fresh process that
      inherits $20 of prior spend and refuses to start spending again
- [x] Text/mock mode always remains available
- [x] Sessions, turns, audit events, escalations, and refill requests survive a restart
- [x] `make budget` reports persisted spend rather than this process's memory
- [x] A database failure degrades to in-memory rather than ending the conversation

A turn is the unit of work: everything a turn produced is written in one transaction at
the end of it. The tradeoff is stated in PROJECT_STATUS — a crash mid-turn loses that
turn's rows, which a regulated deployment would not accept for audit.

---

### ✅ Phase 12 — Cloud provider integrations
`OpenAILLMProvider`, `DeepgramSTTProvider`, `ElevenLabsTTSProvider`. One adapter serves
every OpenAI-compatible endpoint, so Groq's free tier is a settings entry rather than a
fourth implementation.

**Acceptance**
- [x] Each implements its interface and reports usage
- [x] Tool-call arguments validated; invalid output triggers repair/retry, never execution
- [x] CI never invokes a paid API
- [x] First live smoke test costs < $0.25 and is logged in COSTS.md — run 2026-09-09
      against Groq's free tier at **$0.00**

---

### 🟡 Phase 13 — Browser voice
Browser mic → STT → agent → TTS → speaker.

**Acceptance**
- [ ] A full booking completes by voice **in the browser** — needs a transport;
      the decision between LiveKit and a plain WebSocket is open
- [x] Barge-in, silence, and timeout handled by the turn manager
- [x] Voice changes no business logic
- [x] Session cost cap enforced live

The turn manager and the voice session are complete and tested offline: a full booking
runs end to end through scripted STT and counting TTS, with barge-in, queued finals,
silence re-prompts, and the per-session cap all asserted. What is missing is only the
audio transport between a browser and that session.

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
