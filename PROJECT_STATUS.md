# Project status

*Last updated: 2026-09-11*

## Current phase

**Phases 0–14 complete.** The agent works end
to end in text and by voice from a browser microphone, against real Deepgram recognition
and real Groq inference. What it does survives a restart, the dashboard makes every turn
inspectable, and the model now understands the whole conversation rather than only its
first sentence.

Since Phase 13 closed, the work has been almost entirely **conversation repair driven by
recorded calls**: nineteen distinct bugs found by reading transcripts out of
`careline.db`, not by writing tests first. That is worth stating plainly because it is
the honest shape of building this — the test suite was green throughout, and none of the
nineteen were things it occurred to me to test for until a real caller hit them.

Total spend to date: **$0.51**, against a $20 ceiling. Groq's free tier serves the model
and Deepgram serves both recognition and speech from its $200 credit.

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

**Phase 13 — Browser voice** ✅
- `TurnManager`: end-of-utterance detection, barge-in, silence re-prompt then graceful
  close, hard call limit, interim transcripts never acted on, and one in-flight turn per
  session with late finals queued rather than raced
- `VoiceSession`: STT stream in, TTS out, transport-agnostic, with the per-session
  spending cap enforced *during* the call — the caller is offered a human, not hung up on
- Time is injected into the manager, so 21 timing tests run in a third of a second
- `/ws/voice`: a plain WebSocket, with capture in an `AudioWorklet` and playback scheduled
  through Web Audio. Not WebRTC — barge-in needs acoustic echo cancellation, and that
  comes from the browser rather than from the transport (ADR 007, which also records the
  mistaken argument that nearly chose LiveKit)
- `GroqTTSProvider`: Orpheus, measured at ~400 ms to first audio, free. The endpoint
  serves WAV only and streams an unbounded RIFF header, so the adapter strips the
  container as bytes arrive — a chunk-walking state machine, because a byte-offset error
  there does not raise, it turns speech into static
- The transport bug worth naming: cancelling synthesis stops the *server* producing audio,
  but what already crossed the socket is queued in the browser. `on_interrupt` fires
  before cancellation so the client drops it — without that, barge-in leaves the agent
  talking for another second, and no mocked test would catch it

- **Verified from a browser microphone** on 2026-09-10, which is what closed the phase:
  `getUserMedia` capture through an `AudioWorklet`, the agent's reply played back through
  Web Audio, and a barge-in mid-sentence. Two bugs only that run could find — an
  `AudioContext` that starts suspended outside a user gesture, so the page was silent
  with no error, and a carry byte lost when a PCM frame split across reads, which turns
  speech into static rather than raising anything

**Phase 13.5 — Comprehension, and repairing the conversation** ✅ *(unplanned; it came
out of listening to the calls)*

The agent understood the first sentence of a call and then fell back on phrase tables for
everything after it. The recorded turns show it exactly: classification took 300–700 ms
on an opening line and one millisecond on every answer to a question the agent had asked,
because the model was never consulted for those. Every phrasing that failed on a live
call was a one-millisecond turn.

- **The model is asked mid-conversation, when the rules came up empty.** Not otherwise:
  when they have the answer — "the second one", "yes", a date of birth read off a card —
  a round trip buys nothing and those are the turns where a delay is heard. When they do
  not, the alternative was repeating the question, so latency has stopped competing with
  a good answer. It is told which question was asked, and for appointment times, which
  times; an empty slot in a diary is not information about a patient. What appointments
  this caller has and what is on their prescription never leave the process (ADR 008)
- **Out of scope is a thing the model can say** — understood perfectly, and not something
  this line does. A complaint, a test result, notes to a solicitor: measured live, all
  three. Two turns nothing can answer reaches the same place, because the capability menu
  is a fair reply to "hello?" and a poor reply to anything twice
- **The agent will not say the same sentence three times.** The last line of defence and
  the only one that needs no diagnosis: every loop found on a live call looked identical
  from the caller's side, whatever caused it. The third time, it offers a person
- **A finished request lets the next one start.** Workflows kept the state of a request
  that was already over, so a caller who booked, checked the booking, then asked to move
  it got "What would you like to do with your appointment?" six times, whatever they said
- **A question the agent asks is a question it remembers asking.** "Would you like to
  book one?" is put by a workflow that then closes itself, so "yes please" reached the
  menu
- **74 phrasings, written down as a table first** (`tests/unit/test_phrasebook.py`), then
  made to pass — 46 were misrouted. The routing rule is now longest-match-wins, which is
  what the ordinal matcher had already had to learn: a flat table read in order answers
  "which phrase did somebody list first", and that made "Did I book something?" open a
  booking and "Got any cancellations?" cancel one
- **The agent speaks first**, naming the clinic and saying it is automated. A line that
  opens in silence leaves the caller guessing, and the ones who guess wrong open with
  "hello?", which carries no intent

**Phase 14 — Latency** ✅
- Baseline from 148 recorded voice turns: perceived turn **0.38 s** median, STT
  recognition lag 18 ms, safety and routing 5 ms — and speech synthesis **375 ms**, which
  was most of the turn and the only stage outside its budget
- One optimisation, because the measurement named one: synthesis moved to a websocket
  **held open for the call**, measured at **130 ms** to first audio against REST's 445 ms
  (docs/latency.md). Reproduce with `make measure-tts ARGS="--ab --gap 7"`
- The method mattered more than the change, and it caught two of my own errors. A
  comparison taken twenty minutes apart showed a 2.6× win that was mostly the network
  being in a different mood — interleaving the paths in one session is what made the
  numbers mean anything. And with no pause between utterances the socket showed *no
  improvement at all*: a caller speaks for seconds between replies, httpx expires an idle
  connection after five, so a tight benchmark loop measures a warm connection pool that
  no real call has. The fix was to put the gap into the benchmark
- What it is not: raising the HTTP keepalive alone recovers about 110 ms of the 315,
  which is real and far cheaper — but it cannot hold open a connection the server is
  entitled to close, so the socket is opened once and kept
- A held connection brings its own hazard, which is barge-in: an interrupted utterance
  leaves audio queued that nobody read, so the socket is discarded rather than allowed to
  play the interrupted sentence into the middle of the next one

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
| A full booking completes by voice, with no audio hardware | `test_a_booking_completes_by_voice` |
| Barge-in signals the client *before* cancelling playback | `test_the_turn_manager_signals_before_it_cancels` |
| A streamed WAV header is stripped however the reads break | `test_survives_a_header_split_across_reads` |
| Live speech synthesis produces speech, not static | `make smoke-voice` (level 0.053, listened to) |
| The socket opens, negotiates rates, and closes cleanly | driven against a running server |

| Speech during playback cancels it; a cough does not | `TestBargeIn` |
| Interim transcripts never reach the orchestrator | `test_interim_transcripts_never_reach_the_orchestrator` |
| A final arriving mid-turn is queued, never raced | `TestOneTurnAtATime` |
| Silence re-prompts once, then closes gracefully | `TestSilence` |
| The per-session cost cap ends a call in progress | `TestLiveCostCap` |
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

| Every phrasing in the phrasebook routes where it should | `tests/unit/test_phrasebook.py` (74) |
| None of them dead-ends in the capability menu | `TestNothingDeadEnds` |
| A request for a person is heard however it is phrased | `TestAskingForAPerson` |
| The model may fill a gap and never overwrite a parsed value | `tests/unit/test_llm_extraction.py` |
| Every way a model can fail lands back on the rules | `TestTheRulesAreTheFloor` |
| The model is not asked when the rules already answered | `TestTheModelIsAskedMidConversation` |
| It is never sent the record — only which question was asked | `test_it_is_never_told_what_the_options_were` |
| An implausible position from the model is dropped | `TestAPositionIsNotJustANumber` |
| A finished request starts the next one cleanly | `tests/workflows/test_second_request.py` |
| A verified caller is not asked to verify again | `TestAVerifiedCallerIsNotAskedAgain` |
| A day the caller names decides which appointment, or nothing does | `tests/workflows/test_choosing_an_appointment.py` |
| Stale offers from an earlier request resolve nothing | `TestStaleOffersDoNotResolveAChoice` |
| "None of those" offers times they have not refused | `test_rejecting_every_offer_offers_different_times` |
| Saying yes to an offer the agent made does what it offered | `tests/workflows/test_offers_and_handover.py` |
| Two turns nothing can answer offers a person | `TestOfferingAPerson` |
| The same sentence is never said three times | `TestSayingTheSameThingTwice` |
| Changing the subject mid-flow is the model's call alone | `TestChangingTheSubject` |
| The agent greets the caller, and the silence timer starts after it | `TestSpeakingFirst` |
| Speech streams over one socket for the whole call | `TestTheWebsocketPath` |
| An interrupted utterance never leaks into the next one | `test_an_abandoned_utterance_does_not_leak_into_the_next` |
| A socket that will not open falls back to REST rather than silence | `test_a_socket_that_will_not_open_falls_back_to_rest` |
| A vendor error frame is not mistaken for a successful ending | `test_an_error_frame_is_not_a_silent_ending` |
| An abbreviated month is a date, not a name | `TestADateIsNotAName` |

**Verified live on 2026-09-09**, over the real WebSocket against real Deepgram and real
Groq: three spoken utterances in, four turns, one genuine barge-in, 11 seconds of agent
speech back. Time from a final transcript to the first byte of the agent's reply was
**1069–1203 ms**, of which 800 ms is the deliberate end-of-utterance wait — so the
pipeline itself contributes roughly 270–400 ms.

**Not yet verified:** capture from an actual browser microphone. Everything above it is
exercised; the `AudioWorklet` path and `getUserMedia`'s echo cancellation are not.

**One real finding, taken forward into Phase 14.** A date of birth split across two finals — "I was born
on the fourteenth of March nineteen" then "seventy eight" — because Deepgram's
`endpointing=300` finalised mid-number and the halves arrived ~1.1 s apart, wider than the
800 ms window that would have joined them. Verification therefore never completed. This is
the risk this project already documented as the most consequential in the domain, now
measured rather than predicted: the fix is a tuning trade-off (a longer end-of-utterance
window is more robust and slower), and tuning without measurement is guessing.

## Last test results

```
1254 passed in 89s   (full suite, both EHR providers, live HAPI)
1041 passed in 17s   (offline suite: -m "not integration")
```

Try it: `python scripts/text_chat.py --script demo1 --trace`

- The HAPI suite runs against a live FHIR 4.0.1 server via `make test-int`; it skips
  automatically when no server is reachable, so the default suite stays offline.
- 2 warnings = third-party deprecations (starlette/anyio), not project code.
- `ruff check` clean · `ruff format` clean · `mypy` strict clean across 98 source files.
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
6. **Extraction is rules first, model second** (ADR 008). The rules are the floor and
   run on every turn; the model is asked when they come up empty, may fill a gap but
   never overwrite a value they parsed, and cannot be reached at all by a clinical
   question because safety runs before extraction. The failure mode remains "I didn't
   understand", never a wrong action. What the rules still own outright — dates, spoken
   digits, ordinals resolved against a list — they own because they have been hardened
   against real calls and a model has not.
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
15. **Groq's free tier allows 8,000 tokens a minute**, which is roughly sixteen
   classifications. A real call is nowhere near it — one classification per turn, turns
   ten seconds apart — but a test script run end to end without pauses is well over, and
   every call past the limit falls back to the rules. That is the fallback working, and
   it looks exactly like the model getting worse, which cost an hour before the log was
   read properly. Leave a minute between scripted calls.
16. **A description is not a drug name, and the model will offer one anyway.** "The one
   for my sugar" arrives as a prescription named "sugar". The workflows now read the
   record back and ask rather than acting on it, and nothing maps a description to a
   drug: deciding that "the sugar one" is the metformin is a clinical inference, and the
   wrong one reads out the wrong dosage. The cost is that those phrasings take an extra
   turn, and after two the caller is offered a person.
17. **Changing the subject mid-workflow needs the model.** With `AI_MODE=mock`, or when
   the vendor is rate-limited, "actually, sort out my repeat while I'm on" is answered by
   the question the agent asked before it. The rules cannot tell that from a clumsy
   answer, and guessing wrong throws away a booking half made.
18. **`EHRProvider` now has two reads not scoped to one patient** — `list_patients` and
   `list_appointments`, for the dashboard. Nothing in the agent runtime calls them, and a
   caller-facing path that could would defeat verification entirely. That constraint is
   currently a comment on the interface and a code review, not something enforced.

## Next tasks

1. **Deepgram keyword boosting** for the patient roster and the drug names. "Linda
   Newhan", "Fifth John Smith" and "metamorphine" are all recognition errors that every
   layer downstream then has to be robust to, and boosting is the fix at the source.
2. **Deepgram splitting an utterance mid-sentence** — "Can you tell me how much" /
   "metamorphine should I take?" arrived as two turns, and the safety layer then refused
   the fragment. Endpointing is a tuning trade-off and tuning without measurement is
   guessing.
3. **The first utterance of a call still pays 377 ms** to open the speech socket, and it
   is the greeting. Connecting when the call is answered rather than when the agent first
   speaks would hide it; the greeting is also the least latency-sensitive moment in a
   call, which is why it has not been done yet.
4. Phases 15–18: Twilio telephony, the Epic sandbox adapter, the local-model path, and
   the polished demo.

## Architecture decisions

| ADR | Decision |
|---|---|
| [001](docs/decisions/001-fhir-abstraction.md) | EHR access behind a provider interface |
| [002](docs/decisions/002-agent-orchestration.md) | Custom state machine, not an agent framework |
| [003](docs/decisions/003-patient-verification.md) | Session-scoped, server-side verification |
| [004](docs/decisions/004-cloud-ai-providers.md) | Discrete STT → LLM → TTS, vendor-neutral |
| [005](docs/decisions/005-text-vs-voice-testing.md) | Text mode is the primary test surface |
| [006](docs/decisions/006-budget-controls.md) | Budget enforced in software |
| [007](docs/decisions/007-voice-transport.md) | A plain WebSocket for browser audio, not WebRTC |
| [008](docs/decisions/008-model-understands-rules-decide.md) | The model understands; the rules decide |

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
AI_MODE=cloud              # 'mock' makes a paid provider impossible to construct
LLM_PROVIDER=groq  STT_PROVIDER=deepgram  TTS_PROVIDER=deepgram
TEXT_ONLY_MODE=false  STT_ENABLED=true  TTS_ENABLED=true
```

The whole automated test suite runs with `AI_MODE=mock`, which `tests/conftest.py` sets
before anything imports settings — so no test can reach a vendor whatever the developer's
`.env` says. Worth knowing when a script imports from `tests.conftest` for a fixture and
then wonders why the model appears to have stopped working.

## Cost

| | |
|---|---|
| Estimated project spend to date | **$0.51** |
| Warning threshold | $15.00 |
| Ceiling | $20.00 |
| Remaining | **$19.49** |
| Budget status | `ok` |

| Provider | Metric | Quantity | Cost |
|---|---|---|---|
| Deepgram | streaming recognition | 649 s | $0.06 |
| Deepgram | speech synthesis | 14,824 characters | $0.44 |
| Groq | inference (free tier) | 17,333 tokens over 37 requests | $0.00 |
| Groq | speech synthesis (free tier) | 1,687 characters | $0.00 |

Both Deepgram lines are drawn against its $200 credit rather than billed. Groq's free
tier serves the model, which is why an agent that now calls one on most turns still costs
nothing per turn. Check anytime with `make budget`.
