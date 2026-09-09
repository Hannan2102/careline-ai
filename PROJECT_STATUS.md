# Project status

*Last updated: 2026-09-09*

## Current phase

**Phase 3 complete.** Next: Phase 4 (appointment scheduling) — or Phase 8 (safety policies) first, so the safety layer exists before the agent can speak.

## Completed

**Phase 0 — Architecture and repo setup** ✅
- Git repository, monorepo structure with enforced layer separation
- Nine top-level documents, ten deep dives, six ADRs
- `.env.example` covering every switch, defaulting to a configuration that cannot spend money
- `.gitignore` excluding secrets, generated data, and build artifacts
- Docker Compose (PostgreSQL + HAPI FHIR R4), backend Dockerfile, Makefile
- GitHub Actions CI: ruff, ruff format, mypy strict, pytest — offline, mock-only

**Phase 1 — Local EHR foundation** ✅
- `EHRProvider` interface (ADR 001) with two complete implementations
- `MemoryFHIRProvider` — in-process FHIR store, no Docker required
- `LocalFHIRProvider` — HAPI FHIR over REST, with `If-Match` optimistic locking on slots
- FHIR R4 client (search paging, conditional update, transaction bundles)
- Resource ↔ domain mapping, including the verbatim-dosage rule
- Shared scheduling primitives: working hours, lunch break, 15-minute grid, duration fitting
- Curated synthetic clinic and five patients, with two deliberate safety fixtures
- Seeding for both providers from one dataset definition
- FastAPI app: `GET /health`, `GET /api/system/status`
- Usage metering, pricing, and the budget guard
- Structured logging with credential redaction and trace correlation
- **Verified end to end against a running HAPI FHIR 4.0.1 server**

**Phase 2 — FHIR service layer** ✅
- `PatientService` — lookup and registration, with input validation; returns
  candidates without interpreting what a match count means (that is Phase 3)
- `SchedulingService` — reason→type classification, varied slot offers, booking,
  cancellation, rescheduling, and **appointment ownership enforcement**
- `MedicationService` — four distinct lookup outcomes and a verbatim-dosage guard
- One contract suite now runs against **both** EHR providers from a single body of
  test code, which is what actually proves they have not diverged

**Phase 3 — Patient verification** ✅
- `SessionState` with read-only verification: `session.patient_ref = ...` raises
  `AttributeError`, and the only mutator requires a `VerificationDecision` that only
  the verification service produces
- `VerificationService` — name + DOB, second factor on ambiguity, 3-attempt lockout
- `require_verified_patient` — the gate every PHI-shaped operation passes through
- `EscalationService` — structured handoffs; failed verification routes to the front
  desk (Phase 8 adds the clinical policies)
- `SessionStore` with TTL expiry, so a verified session cannot be inherited later

## What actually works — and how I know

| Capability | Verified by |
|---|---|
| Every EHR assertion holds on **both** providers | `tests/integration/test_ehr_contract.py` (32 × 2) |
| Every service rule holds on **both** providers | `tests/integration/test_services.py` (32 × 2) |
| A patient cannot cancel or move another's appointment | `TestAppointmentOwnership` |
| A refused change leaves the owner's booking intact | `test_a_refused_cancellation_leaves_the_appointment_booked` |
| Medication lookup separates not-found from ambiguous from no-dosage | `TestMedicationService` |
| Altered dosage wording is rejected by the verbatim guard | `test_the_verbatim_guard_rejects_altered_wording` |
| Reason → appointment type, with a flagged fallback | `tests/unit/test_appointment_classification.py` |
| HAPI's own optimistic locking refuses a stale write | `tests/integration/test_hapi_server.py` |
| Verification state cannot be assigned, only decided | `tests/unit/test_session_state.py` |
| Wrong DOB and unknown name produce identical responses | `test_a_wrong_date_of_birth_is_indistinguishable...` |
| Ambiguous matches never reveal candidates or their count | `test_the_candidates_are_never_revealed` |
| A second factor matching several records verifies nobody | `test_a_second_factor_matching_several_records_is_refused` |
| 3 failed attempts lock the session and escalate | `TestLockout` |
| A locked session refuses even correct details | `test_a_locked_session_stops_accepting_attempts` |
| Every PHI operation refuses without verification | `TestPhiOperationsThroughTheGate` |
| A model-supplied patient id for another patient is refused | `test_a_mismatched_patient_reference_is_refused` |
| Ending or expiring a session revokes access | `test_ending_the_session_closes_the_gate` |
| Patient search: exact, unknown, ambiguous, wrong DOB | `tests/integration/test_memory_ehr_provider.py::TestPatientSearch` |
| Booking with type-driven duration and slot consumption | `TestBooking` |
| Double-booking refused; 5 concurrent bookings → exactly 1 winner | `test_concurrent_booking_of_one_slot_has_exactly_one_winner` |
| Patient cannot hold two overlapping appointments | `test_patient_cannot_hold_two_overlapping_appointments` |
| Cancellation releases slots | `TestCancellation` |
| Reschedule frees old + books new; failure leaves original intact | `TestRescheduling` |
| Verbatim dosage; missing dosage yields `None`, never a reconstruction | `tests/unit/test_fhir_mappings.py::TestDosageIsVerbatim` |
| Unknown appointment type rejected, not coerced | `test_unknown_appointment_type_is_rejected_not_defaulted` |
| No weekend slots; no appointment straddling the break or closing | `tests/unit/test_scheduling_rules.py` |
| Budget guard at $14.99 / $15.00 / $19.99 / $20.00 | `tests/unit/test_budget_guard.py::TestProjectThresholds` |
| Per-session cost, TTS, and STT caps trip independently | `TestSessionCeilings` |
| Override honoured in development, ignored in production | `TestOverride` |
| Paid provider without a key fails at startup | `tests/unit/test_settings.py` |
| `/health` and `/api/system/status` | `tests/integration/test_api.py` |

## Last test results

```
294 passed in 88.0s   (full suite, both EHR providers)
202 passed in  2.6s   (offline suite: -m "not integration")
```

- The HAPI suite runs against a live FHIR 4.0.1 server via `make test-int`; it skips
  automatically when no server is reachable, so the default suite stays offline.
- 2 warnings = third-party deprecations (starlette/anyio), not project code.
- `ruff check` clean · `ruff format` clean · `mypy` strict clean across 40 source files.
- Zero network calls, zero cost.

## Known problems and gaps

1. **Integration tests reset HAPI per test.** 64 HAPI tests take ~40 s. Acceptable now;
   a session-scoped baseline with per-test cleanup would scale better.
2. **The offline suite slows dramatically under memory pressure.** With HAPI's JVM
   resident on a 8 GB machine it went from 1.0 s to 117 s while the system swapped. Worth
   knowing before blaming the tests; `make down` when not using the FHIR server.
3. **Ownership is checked against *booked* appointments only.** Cancelling an already
   cancelled appointment reports "not owned" rather than "already cancelled". Correct and
   safe, but the workflow will want the clearer message in Phase 5.
4. **Sessions and escalations are in-process.** They vanish on restart and are not visible
   across processes. Phase 11 persists both.
5. **Name + DOB remains weak authentication**, as SAFETY.md states. The second factor is
   requested only on ambiguity, not always — matching common clinic practice, not good
   security. A real deployment needs more.
3. **The 15-minute slot grid rounds durations up.** A 20-minute visit occupies 30 minutes of
   grid. Documented in `docs/fhir-data-model.md`; a real template model would fix it.
4. **No application database yet.** `session`, `turn`, `audit_event`, `escalation`,
   `refill_request`, and `provider_usage` are designed but not implemented; the usage
   ledger is in-memory and resets with the process (Phase 11).
5. **Name + DOB is weak authentication.** Deliberate, matching common clinic practice, and
   called out as a gap in SAFETY.md rather than glossed over.
6. **`get_practitioners` reads the curated roster** rather than querying `PractitionerRole`.
   Fine for a fixed three-provider clinic; revisit if the roster becomes dynamic.

## Next tasks

1. **Phase 8 before Phase 4–7** — the safety policies. Everything from here adds
   capability; the safety layer decides what the agent may do with it, and it should
   exist before there is an agent to constrain.
2. **Phase 4/5** — booking and appointment-management workflows over the services.
3. **Phase 9 last of that group** — safety policies land before the agent can talk, so there is
   never a build in which the agent answers a clinical question.

## Architecture decisions

| ADR | Decision |
|---|---|
| [001](docs/decisions/001-fhir-abstraction.md) | EHR access behind a provider interface |
| [002](docs/decisions/002-agent-orchestration.md) | Custom state machine, not an agent framework |
| [003](docs/decisions/003-patient-verification.md) | Session-scoped, server-side verification |
| [004](docs/decisions/004-cloud-ai-providers.md) | Discrete STT → LLM → TTS, vendor-neutral |
| [005](docs/decisions/005-text-vs-voice-testing.md) | Text mode is the primary test surface |
| [006](docs/decisions/006-budget-controls.md) | Budget enforced in software |

One deliberate deviation from the original specification: **`create_refill_request` is not
on the `EHRProvider` interface.** A refill request is a workflow artifact awaiting
clinician review, not a clinical order; writing it into the EHR as a `MedicationRequest`
would misrepresent it as a prescription — the exact confusion SAFETY.md forbids. It lives
in the application schema and remains a first-class agent tool. Reasoning in
[FHIR.md](FHIR.md).

## Current provider configuration

```
APP_ENV=development
EHR_PROVIDER=memory        # 'local' for HAPI
AI_MODE=mock               # no paid provider can be constructed
LLM_PROVIDER=mock  STT_PROVIDER=mock  TTS_PROVIDER=mock
TEXT_ONLY_MODE=true  VOICE_ENABLED=false  STT_ENABLED=false  TTS_ENABLED=false
```

## Cost

| | |
|---|---|
| Estimated project spend to date | **$0.00** |
| Warning threshold | $15.00 |
| Ceiling | $20.00 |
| Remaining | **$20.00** |
| Budget status | `ok` |
| Provider usage | none — no paid API has been called |

No OpenAI, Deepgram, ElevenLabs, LiveKit, or Twilio call has been made at any point.
Phase 1 needed none, and the default configuration makes one impossible. Check anytime
with `make budget`.
