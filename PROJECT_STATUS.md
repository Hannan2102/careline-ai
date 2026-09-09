# Project status

*Last updated: 2026-09-09*

## Current phase

**Phases 0–12 complete.** The agent works end to end in text, what it does survives a restart, the dashboard makes every turn inspectable, and it can now reach a real model — verified live against Groq on 2026-09-09. Total spend to date: **$0.00**. Next: Phase 13 (LiveKit browser voice).

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

**Phase 9 — Text agent interface** ✅
- `Orchestrator` — classify safety, extract, route, execute, render, record. Safety
  is first and unconditional; workflows hang off the ALLOW branch only
- `RuleBasedExtractor` behind a `TurnExtractor` protocol — deterministic and free;
  Phase 12 adds an LLM implementation of the same seam
- `TurnTrace` — transcript, intent, entities, workflow, safety decision, record
  operations, per-stage latency, estimated cost. Identifiers are redacted: a debug
  record should not become a second place patient data accumulates
- `ClinicFaqWorkflow` — structured lookup, no RAG
- `scripts/text_chat.py` with `--trace` and six replayable demo scripts
- `POST /api/agent/sessions/{id}/turns` — the single entry point the CLI and, later,
  the voice agent both call

**Phase 11 — Persistence, usage tracking, and budget controls** ✅
- Six-table application schema; async SQLAlchemy over PostgreSQL, or SQLite without
  Docker — the same two-tier arrangement as the EHR layer, for the same reason
- A turn is the unit of work: one transaction at the end of each turn
- Spend is carried forward at startup, so the $20 ceiling means *this project* and
  not *this boot* — a fresh process inheriting $20 refuses to spend, and that is tested
- `make budget` reads the persisted ledger
- A database failure logs and degrades to in-memory: the caller is on the phone

**Phase 10 — Admin dashboard** ✅
- Verified live, not only in tests: four scripted conversations driven through the CLI,
  read back through every dashboard endpoint, with a booking made against a real HAPI
  server appearing on the Appointments page
- Next.js App Router + TypeScript strict + Tailwind: Overview, Calls, Agent Trace,
  Patients, Appointments, Escalations
- Agent Trace renders **every** field of a persisted turn — transcript, safety decision
  and the rule that fired, intent and confidence, entities, workflow and its state, each
  audited record operation, per-stage latency, and estimated cost
- Audit events now carry the `turn_id` that produced them, which is what lets the trace
  attribute record operations to the exchange that caused them
- Backend read APIs: `/api/calls`, `/api/calls/{id}/trace`, `/api/escalations`,
  `/api/medications/refill-requests`, `/api/patients`, `/api/appointments`,
  `/api/providers`, `/api/overview`, `/api/usage/summary`
- Two staff-facing reads added to `EHRProvider` (`list_patients`, `list_appointments`),
  held to the same both-providers contract suite as everything else
- The synthetic-data banner is fixed to every route and cannot be dismissed
- Read-only: no staff action exists, because no staff authentication exists
- Two bugs the live run found that the tests had not: `make chat` persisted nothing
  (the CLI built a runtime with no database, so a terminal conversation could never
  reach the dashboard), and ending a session wrote its *revoked* state over the row,
  erasing which patient the call had been about. Both fixed, both now covered

**Phase 12 — Cloud provider integrations** ✅
- `OpenAILLMProvider` (chat completions and tool calls), `DeepgramSTTProvider`
  (streaming), `ElevenLabsTTSProvider` (streaming) — over `httpx` and `websockets`
  rather than vendor SDKs
- A provider factory: the only module that imports a concrete provider, and the only
  place a paid one can be constructed
- Paid providers are wrapped so the budget guard is consulted before **every** call, not
  only at construction — a provider is built once and used for a whole conversation
- Blocking falls back to the mock and answers, rather than raising mid-conversation
- Tool-call validation with a bounded repair loop: no coercion, no partial execution, and
  a `patient_id` the model supplies is checked against the session's verified reference
- `make smoke-cloud` is the only path that can spend money, and refuses four ways
- Groq's free tier as an LLM provider: the same adapter with a different base URL, which
  is the provider abstraction earning its keep
- **Verified live** on 2026-09-09 against `openai/gpt-oss-20b`, at $0.00. Extraction runs
  in 130–500 ms — inside the latency budget — and, given "The first one please", returns
  nothing rather than inventing a patient. A local `llama3.2:3b` given the same input
  fabricated `full_name: "John Doe", date_of_birth: "1990-05-15"`, which is why the
  measurement was worth taking before trusting a model with verification inputs.

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
| Six DEMO.md scenarios run end to end in text | `TestDemoScenarios` |
| A refusal performs zero record operations | `test_safety_runs_before_any_workflow` |
| A refusal mid-booking leaves the EHR untouched | `test_a_refusal_mid_booking_stops_the_workflow` |
| Prompt injection never reaches a workflow | `test_injection_does_not_reach_a_workflow` |
| "Yes" mid-booking continues, never restarts | `test_an_in_progress_workflow_keeps_the_turn` |
| "Tuesday please" resolves against the offered times | `test_a_weekday_resolves_against_the_offered_times` |
| Traces carry no names or dates of birth | `test_the_trace_does_not_accumulate_identifiers` |
| An unrecognised request offers the menu, never guesses | `test_an_unrecognised_request_offers_the_menu_rather_than_guessing` |
| Turns, audit, escalations, refills, usage all persist | `tests/integration/test_persistence.py` |
| Persisted turns and audit rows carry no identifiers | `test_persisted_turns_carry_no_identifiers` |
| A fresh process with $20 already spent refuses to spend | `test_carried_forward_spend_can_block` |
| A database failure does not end the conversation | `test_a_persistence_failure_does_not_break_the_conversation` |
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
| Each cloud adapter, driven against a faked transport | `tests/unit/test_cloud_providers.py` |
| Usage metered from the vendor's own numbers, not estimated | `test_usage_comes_from_the_response_not_an_estimate` |
| A transient 503 retries once; a 400 never does | `test_a_transient_failure_is_retried_once` |
| A rejected key is reported without echoing it | `test_a_rejected_key_says_so_without_echoing_it` |
| Unparseable tool arguments are dropped, never guessed at | `test_unparseable_arguments_are_dropped_not_guessed_at` |
| `AI_MODE=mock` builds a mock even with a real key present | `test_mock_mode_cannot_build_a_paid_provider_even_with_a_key` |
| The ceiling falls back mid-conversation and spends nothing | `TestPerCallGuard` |
| Invalid tool arguments are rejected, never defaulted | `tests/unit/test_tool_call_validation.py` |
| A patient id for someone else is refused, indistinguishably | `test_the_refusal_does_not_say_whether_the_patient_exists` |
| The repair loop is bounded, per proposal | `TestRepair` |
| Groq's rejection of `messages[].name` is handled, not discovered | `TestGroqCompatibility` |
| `reasoning_effort` is sent to models that have it and no others | `test_reasoning_effort_is_absent_for_a_model_without_it` |
| A rate limit degrades to the deterministic path instead of ending a call | `TestDegradingOnProviderFailure` |
| A free provider is never blocked by a full budget ceiling | `test_a_full_ceiling_does_not_block_a_free_provider` |
| Every dashboard endpoint, against data a real turn produced | `tests/integration/test_dashboard_api.py` |
| The trace exposes every field of a turn record | `test_every_turn_field_is_present` |
| Record operations are attributed to the turn that caused them | `test_operations_are_attributed_to_their_turn` |
| The roster and schedule reads agree on **both** providers | `TestStaffReads` |
| The dashboard says why it is empty when persistence is off | `test_the_read_apis_say_why_they_are_empty` |
| Dashboard lint, `tsc --noEmit` (strict), and `next build` | CI job `dashboard` |
| Ending a call keeps the patient it was about | `test_an_ended_call_keeps_the_patient_it_was_about` |
| A trailing "yes" does not make a call read as "unknown" | `test_the_last_meaningful_intent_is_shown` |

## Last test results

```
677 passed in 45.7s   (full suite, both EHR providers)
477 passed in  3.4s   (offline suite: -m "not integration")
```

Try it: `python scripts/text_chat.py --script demo1 --trace`

- The HAPI suite runs against a live FHIR 4.0.1 server via `make test-int`; it skips
  automatically when no server is reachable, so the default suite stays offline.
- 2 warnings = third-party deprecations (starlette/anyio), not project code.
- `ruff check` clean · `ruff format` clean · `mypy` strict clean across 83 source files.
- Dashboard: `eslint` clean · `tsc --noEmit` (strict, `noUncheckedIndexedAccess`) clean ·
  `next build` clean.
- Zero network calls, zero cost.

## Known problems and gaps

1. **Integration tests reset HAPI per test**, but incrementally: the reset frees the
   slots a test actually dirtied rather than rewriting the ~960-slot grid, and falls back
   to a full seed only when the dataset is absent or covers the wrong dates. That took the
   full suite from 145 s to 59 s.
2. **The offline suite slows dramatically under memory pressure.** With HAPI's JVM
   resident on a 8 GB machine it went from 1.0 s to 117 s while the system swapped. Worth
   knowing before blaming the tests; `make down` when not using the FHIR server.
3. **Ownership is checked against *booked* appointments only.** Cancelling an already
   cancelled appointment reports "not owned" rather than "already cancelled". Correct and
   safe, but the workflow will want the clearer message in Phase 5.
4. **Audit events are flushed at the end of a turn, not at the point of access.** A
   crash mid-turn loses that turn's rows. This was a deliberate trade: writing each event
   synchronously would push async plumbing through the safety layer and verification
   service for no benefit the demo can measure. A regulated deployment would not accept
   it, and the fix is a synchronous audit write at the point of access.
5. **Name + DOB remains weak authentication**, as SAFETY.md states. The second factor is
   requested only on ambiguity, not always — matching common clinic practice, not good
   security. A real deployment needs more.
6. **Extraction is rule-based.** It handles the demo phrasings and the obvious
   variations, and it will miss paraphrases no rule anticipated — the failure mode is
   "I didn't understand", never a wrong action, because safety runs before extraction.
   The LLM extractor in Phase 12 is what makes this robust.
7. **Safety detection is phrase-based.** It will miss paraphrases no rule anticipates —
   testing caught exactly that with "ending my life" against a literal "end my life", now
   fixed with inflection-aware patterns. The model-flag layer exists to cover the gap, and
   the honest position is that this needs clinical review, adversarial testing, and real
   transcripts before anyone would trust it.
8. **Sessions live in one process.** The API's runtime is a module-level singleton, so a
   second worker would not see the first's live sessions — persisted rows are shared, the
   in-flight conversation state is not. Fine for local development; a real deployment
   needs session state in the database or a shared store.
9. **No migrations.** Schema is created with `create_all`, because every environment
   builds it from scratch and there is nothing to migrate yet. Alembic belongs here the
   moment a deployed database must survive a schema change.
10. **The 15-minute slot grid rounds durations up.** A 20-minute visit occupies 30 minutes
   of grid. Documented in `docs/fhir-data-model.md`; a real template model would fix it.
11. **`get_practitioners` reads the curated roster** rather than querying `PractitionerRole`.
   Fine for a fixed three-provider clinic; revisit if the roster becomes dynamic.
12. **The dashboard is unauthenticated and read-only.** It exposes every synthetic chart
   and every call to anyone who can reach the port, which is acceptable only because it
   is local-only and the data is invented. It is also why there is no "approve refill" or
   "resolve escalation" button: a staff action needs a staff identity, and there is none.
   Authentication and role-based access are the prerequisite, not the button.
13. **With `EHR_PROVIDER=memory`, the CLI and the API do not share an EHR.** Each process
   holds its own in-process store, so a booking made in `make chat` will not appear on the
   dashboard's Patients or Appointments pages — the *call*, trace, and escalations will,
   because those go through the shared database. Running against HAPI
   (`EHR_PROVIDER=local`) shares the record and behaves as expected; this is verified
   both ways. It is the in-process provider's nature, not a defect, but it is a
   sharp edge in a demo and the docs now say so.
14. **A broken CA trust store, resolved.** Early in the project every HTTPS-capable
   client failed at construction with `X509: NO_CERTIFICATE_OR_CRL_FOUND`, including
   plain-HTTP calls to the local HAPI server — httpx builds an SSL context regardless of
   scheme. `uses_tls()` in `app/fhir/client.py` fixed the symptom by not loading a trust
   store for `http://`, and that remains correct on its own merits. The cause was almost
   certainly the venv living in an iCloud-synced folder: `certifi/cacert.pem` had been
   evicted to a cloud stub, so Python read an empty CA file. After moving the project off
   the Desktop and rebuilding the venv, `ssl.create_default_context()` loads 128 CA
   certificates and a verified TLS handshake to `api.openai.com` succeeds. Worth knowing
   because it is invisible: an evicted file has the right name, path, and permissions.
15. **`EHRProvider` now has two reads not scoped to one patient** — `list_patients` and
   `list_appointments`, for the dashboard. Nothing in the agent runtime calls them, and a
   caller-facing path that could would defeat verification entirely. That constraint is
   currently a comment on the interface and a code review, not something enforced.

## Next tasks

1. **Phase 12 — cloud AI providers.** The first phase that can spend money. The budget
   guard, the persisted ledger, and the carried-forward baseline all exist precisely so
   that this phase cannot quietly run past $20.

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
