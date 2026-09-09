# Project status

*Last updated: 2026-09-08*

## Current phase

**Phase 1 — Local EHR foundation.** Phase 0 (architecture and repo) is complete.

## Completed

**Phase 0 — Architecture and repo setup** ✅
- Git repository, monorepo structure with enforced layer separation
- Nine top-level documents, ten deep dives, six ADRs
- `.env.example` covering every switch, defaulting to a configuration that cannot spend money
- `.gitignore` excluding secrets, generated data, and build artifacts
- Docker Compose (PostgreSQL + HAPI FHIR R4), backend Dockerfile, Makefile
- GitHub Actions CI: ruff, ruff format, mypy strict, pytest — offline, mock-only

**Phase 1 — Local EHR foundation** (mostly complete)
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

## What actually works — and how I know

| Capability | Verified by |
|---|---|
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
103 passed, 6 skipped, 2 warnings in 0.67s
```

- 6 skipped = HAPI integration tests, skipped because no FHIR server is reachable.
- 2 warnings = third-party deprecations (starlette/anyio), not project code.
- `ruff check` clean · `ruff format` clean · `mypy` strict clean across 40 source files.
- Zero network calls, zero cost.

## Known problems and gaps

1. **HAPI FHIR is unverified on this machine.** Docker is not installed here, so
   `docker-compose.yml` and `LocalFHIRProvider` have **not** been run against a live
   server. The compose file's YAML parses and the provider satisfies the interface and
   type checks, but the Phase 1 acceptance criterion "verified against a running HAPI
   server" is **not met**. `tests/integration/test_local_fhir_provider.py` exists and will
   exercise it; it currently skips. This is the single most important thing to verify next.
2. **`MemoryFHIRProvider` could drift from HAPI.** Mitigated by shared primitives, shared
   mappings, and mirrored test assertions — but only running both proves it.
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

1. Install Docker, run `make up && make wait-fhir`, confirm `/fhir/metadata`, seed HAPI,
   and run `make test-int`. Close the open Phase 1 criterion.
2. **Phase 2** — domain services (`patient_service`, `scheduling_service`,
   `medication_service`) over the EHR interface.
3. **Phase 3** — verification service and session state.
4. **Phase 8 before Phase 9** — safety policies land before the agent can talk, so there is
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
