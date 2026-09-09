# Project status

*Last updated: 2026-09-09*

## Current phase

**Phases 0–8 complete.** Next: Phase 9 (text agent interface) — the orchestrator that makes safety-before-dispatch structural rather than compositional.

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

**Phase 8 — Safety and escalation** ✅ *(built out of order, ahead of 4–7)*
- Nine safety categories, each with a rule, a fixed route, a positive test and a
  negative test
- Urgent symptoms outrank everything: "chest pain — should I take more?" is an
  emergency, not a dosing question
- Refusal text is fixed, not generated, and checked to contain no medical content
- `SafetyService` produces the structured handoff, preserving the patient's own words
- Two asymmetric layers: deterministic rules and an optional model flag, where either
  can refuse and neither can grant permission the other denied

**Phase 4 — Appointment scheduling** ✅
- `ExistingPatientBookingWorkflow` — seven named states, every transition tested
- Takes **typed input**, not free text: turning an utterance into those fields is the
  orchestrator's job (Phase 9), which is why every branch tests without a model
- Handles ambiguity, lockout, unknown clinicians, rejected offers, declined
  confirmations, a slot taken mid-conversation, and an unreachable EHR
- Workflow memory is namespaced and JSON-serialisable, so a booking survives a
  round trip to storage

**Phase 5 — Appointment management** ✅
- `AppointmentManagementWorkflow` — lookup, cancel, reschedule, with disambiguation
  when the patient has more than one appointment
- A lookup answers when, who with, and where in one sentence
- A lost reschedule race leaves the original appointment in place and says so
- `AuditService` — append-only trail of every access and mutation, recording *that* a
  record was touched and by which session, never the clinical content itself
- Booking (Phase 4) was retrofitted to audit as well, for consistency

**Phases 6 & 7 — Medication retrieval and refill requests** ✅
- `MedicationLookupWorkflow` — reads the stored instruction back verbatim, and treats
  not-found, ambiguous, and no-dosage-on-file as three different situations
- `RefillRequestWorkflow` + `RefillService` — creates requests in `PENDING_REVIEW`
  and has no code path that could approve one
- `IdentityCollector` extracted: identity handling was duplicated across workflows,
  and divergence in a security-relevant path is how guarantees quietly leak. One
  implementation, with a test asserting all four workflows reply identically.

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
| 33 unsafe utterances refused and correctly categorised | `test_unsafe_requests_are_refused_and_categorised` |
| 16 ordinary requests **not** over-blocked | `test_ordinary_requests_are_allowed` |
| No refusal contains medical content | `test_no_refusal_contains_medical_content` |
| Six prompt-injection attempts change nothing | `test_conversation_text_cannot_disable_the_rules` |
| A model flag cannot downgrade a deterministic refusal | `test_a_model_cannot_remove_a_deterministic_refusal` |
| The Lisinopril/dizziness scenario yields a full handoff | `TestTheCanonicalScenario` |
| Asking for a stored dosage stays allowed | `test_asking_for_a_stored_dosage_is_not_a_dose_change` |
| A full booking conversation, turn by turn | `TestHappyPath::test_a_full_booking_conversation` |
| Two sessions confirming one slot → one booking, one re-offer | `test_two_sessions_confirming_the_same_slot_yield_one_booking` |
| A slot taken mid-conversation re-offers rather than fails | `test_a_slot_taken_mid_conversation_is_handled_gracefully` |
| Booking is unreachable before verification | `test_booking_cannot_start_without_identity` |
| Wrong DOB and unknown name reprompt identically | `test_wrong_details_reprompt_without_revealing_anything` |
| A half-finished booking survives serialisation | `test_a_half_finished_booking_is_serialisable` |
| A clinical question mid-booking refuses and leaves it resumable | `TestSafetyComposition` |
| Cancelling frees the slot for rebooking | `test_cancelling_releases_the_slot` |
| Rescheduling moves the booking and frees the old time | `test_rescheduling_moves_the_appointment_and_frees_the_old_slot` |
| A lost reschedule race keeps the original appointment | `test_a_taken_slot_leaves_the_original_appointment_in_place` |
| Several appointments are disambiguated before acting | `test_several_appointments_are_disambiguated_before_acting` |
| Cancellation needs explicit confirmation | `test_cancellation_requires_explicit_confirmation` |
| Reads, writes, and denials are all audited | `TestAuditTrail` |
| The audit trail contains no names, dates of birth, or drugs | `test_the_audit_trail_holds_no_clinical_content` |
| Dosage read back matches the EHR byte for byte | `test_the_answer_is_not_paraphrased` |
| A prescription with no instruction escalates clinically | `test_a_missing_instruction_escalates_instead_of_guessing` |
| Listing medications gives names without dosages | `test_listing_medications_gives_names_without_dosages` |
| A refill is sent for review, never described as approved | `test_a_refill_is_sent_for_review_not_approved` |
| A refill leaves the EHR provably unchanged | `test_a_refill_never_becomes_a_prescription` |
| `RefillService` has no method that could authorise | `test_the_refill_service_has_no_way_to_authorise` |
| All four workflows reprompt for identity identically | `test_every_workflow_reprompts_identically` |
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
554 passed in 145.5s  (full suite, both EHR providers)
397 passed in   2.0s  (offline suite: -m "not integration")
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
4. **Sessions, escalations, and audit events are in-process.** They vanish on restart
   and are not visible across processes. Phase 11 persists all three — and an audit trail
   that does not survive a restart is not really an audit trail, which is why that phase
   matters more than its position in the list suggests.
5. **Name + DOB remains weak authentication**, as SAFETY.md states. The second factor is
   requested only on ambiguity, not always — matching common clinic practice, not good
   security. A real deployment needs more.
6. **Safety detection is phrase-based.** It will miss paraphrases no rule anticipates —
   testing caught exactly that with "ending my life" against a literal "end my life", now
   fixed with inflection-aware patterns. The model-flag layer exists to cover the gap, and
   the honest position is that this needs clinical review, adversarial testing, and real
   transcripts before anyone would trust it.
7. **Nothing calls the classifier yet.** It runs before dispatch by construction once the
   orchestrator exists (Phase 9).
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

1. **Phase 9** — safety policies land before the agent can talk, so there is
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
