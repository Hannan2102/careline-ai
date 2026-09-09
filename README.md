# agentic-patient-access

**CareLine AI** — an agentic AI patient-access platform for a fictional outpatient clinic,
*Oakwood Family Medicine*.

> ⚠️ **Portfolio / demonstration project.** All patient data is **synthetic**. No real
> patient information is used, stored, or transmitted. This project is **not HIPAA
> compliant** and is not intended for clinical use. It demonstrates an *architecture*
> that could be hardened for a regulated environment — see [SAFETY.md](SAFETY.md).

---

## What this is

An AI receptionist that resolves routine **patient-access** workflows end to end —
booking, rescheduling, cancelling, medication instruction lookup, refill requests,
clinic FAQs — by calling **typed tools** against a **FHIR R4** EHR, and that **escalates
to a human** the moment a request crosses into clinical territory.

The framing matters:

> This is **not** "ChatGPT answers clinic calls."
> It is **an AI agent that securely resolves patient-access workflows by interacting with
> healthcare systems through constrained tools.**

The LLM decides *what the caller wants* and *how to say things*. Deterministic Python
decides *what is allowed*, *what the record says*, and *what gets written back*.

## The problem

Outpatient clinics lose a large share of front-desk capacity to a small set of repetitive
calls: "when is my appointment", "I need to move it", "how much of my metformin do I
take", "I need a refill", "are you open Saturday". These calls are high volume, low
complexity, and — critically — **occasionally not administrative at all**. A system that
automates them has to be excellent at the boring 90% *and* reliably recognise the 10%
where a human clinician must be involved.

Inspired by the problem space that Assort Health, Syllable, Hyro, Artera, Talkdesk
Healthcare, Notable, and Hippocratic AI operate in. The implementation and architecture
here are my own.

## How it differs from a generic chatbot

| Generic chatbot | This system |
|---|---|
| Answers from model weights | Answers from the EHR; dosages are read **verbatim** from `MedicationRequest` |
| Free-form text in, text out | Typed, Pydantic-validated tool calls; malformed arguments never execute |
| No identity concept | Session-scoped patient verification gates every PHI-shaped read |
| "Helpfully" answers medical questions | Deterministic safety policies refuse and escalate with a structured handoff |
| Stateless | Stateful conversation + slot-hold + audit trail per turn |
| Opaque | Full agent trace: transcript → intent → workflow → safety decision → tool I/O → latency → cost |

## Architecture at a glance

```mermaid
flowchart TD
  A["Phone (future) / Browser mic / Text console"] --> B[Realtime transport - LiveKit]
  B --> C[STTProvider]
  C --> D[Conversation runtime]
  D --> E[Agent orchestrator]
  E --> F[Specialised workflows]
  E --> G[Deterministic safety layer]
  F --> H[Typed tool layer - Pydantic]
  G --> H
  H --> I[Domain services]
  I --> J[EHRProvider interface]
  J --> K[LocalFHIRProvider - HAPI FHIR R4]
  J --> L[MemoryFHIRProvider - no Docker]
  J --> M[EpicFHIRProvider - future]
  I --> N[(PostgreSQL - sessions, audit, escalations, usage)]
  D --> O[TTSProvider]
  O --> B
```

Full detail, including the text-mode path, the browser-voice path, and the future
Twilio path: **[ARCHITECTURE.md](ARCHITECTURE.md)**.

## Current status

**Phase 1 of 18** — foundation. See [PROJECT_STATUS.md](PROJECT_STATUS.md) for the
authoritative, continuously-updated status, and [ROADMAP.md](ROADMAP.md) for phase
acceptance criteria.

Working today:

- Repo, architecture, and decision records
- FastAPI backend with `/health` and `/api/system/status`
- `EHRProvider` interface + two implementations: `MemoryFHIRProvider` (no Docker) and
  `LocalFHIRProvider` (HAPI FHIR R4 over REST)
- FHIR R4 client and resource ↔ domain-model mappings
- Curated synthetic clinic + patients, loadable into either provider
- Budget/usage configuration and guard (no paid API calls are possible yet)
- Test suite, green, with zero network and zero cost

Not built yet: the agent runtime, workflows, safety policies, dashboard, and every
voice provider. Those are Phases 3–18 and are deliberately *not* stubbed with fake
behaviour.

## Technology

**Backend** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy (async), httpx
**Frontend** Next.js, React, TypeScript (strict), Tailwind
**Data** PostgreSQL, HAPI FHIR R4, Synthea for bulk synthetic patients
**AI (cloud)** OpenAI (LLM + tool calling), Deepgram (STT), ElevenLabs (TTS)
**AI (local, future)** Ollama/Qwen3, faster-whisper, Piper
**Voice** LiveKit browser audio first; Twilio + SIP later
**Ops** Docker Compose, GitHub Actions, ruff, mypy, pytest

Every AI provider sits behind an interface. No business logic imports a vendor SDK —
see [docs/provider-abstraction.md](docs/provider-abstraction.md).

## Cost

Total initial development budget: **$15–$20**, enforced in software, not by discipline.

| | |
|---|---|
| HAPI FHIR, PostgreSQL, Synthea, Docker, FastAPI, Next.js | $0 |
| LiveKit, GitHub | free tier |
| OpenAI | ~$7 |
| Deepgram | ~$4 |
| ElevenLabs | ~$4 |
| Contingency | $0–$5 |

~90–95% of development happens in **text mode**, which makes zero paid calls. Voice APIs
are reserved for a handful of short final demos. Usage is metered per provider, costed,
and a **budget guard** blocks optional paid calls at the ceiling. Details and the
tracking schema: **[COSTS.md](COSTS.md)**.

## Local setup

Prerequisites: Python 3.12 (or [uv](https://docs.astral.sh/uv/)), Node 20+, and — for the
full stack — Docker Desktop.

```bash
git clone <your-fork> agentic-patient-access
cd agentic-patient-access
cp .env.example .env          # defaults are text-only and cost $0
make install
```

### Without Docker (fastest path)

`EHR_PROVIDER=memory` runs an in-process synthetic FHIR store. Everything except the HAPI
integration tests works.

```bash
make seed        # load the curated clinic + demo patients
make dev         # http://localhost:8000/docs
make test
```

### With Docker (full stack)

```bash
make up          # postgres + hapi-fhir
make wait-fhir   # blocks until /fhir/metadata answers
make seed EHR_PROVIDER=local
make dev
```

| Command | Does |
|---|---|
| `make up` / `make down` | start / stop infrastructure |
| `make logs` | tail infrastructure logs |
| `make reset` | destroy volumes, recreate, reseed |
| `make seed` | load curated clinic, providers, patients |
| `make dev` | run the FastAPI backend |
| `make test` | run the test suite (no network, no cost) |
| `make budget` | print estimated spend and remaining budget |
| `make lint` / `make typecheck` / `make fmt` | ruff / mypy / format |

### Text mode

Text mode is the default and shares **the same workflows** as voice — there is no second
implementation of the business logic.

The agent runtime and its CLI (`scripts/text_chat.py`) arrive in **Phase 9**. Until then
the foundation is exercised through the API and the test suite:

```bash
make dev                                   # http://localhost:8000/docs
curl -s localhost:8000/api/system/status   # EHR, providers, modes, budget
```

### Voice mode

Voice requires explicit opt-in in `.env`, and the budget guard must have headroom:

```env
TEXT_ONLY_MODE=false
VOICE_ENABLED=true
STT_ENABLED=true
TTS_ENABLED=true
AI_MODE=cloud
```

### Disabling paid providers

Set `AI_MODE=mock` (or leave the three `*_PROVIDER` values at `mock`). Deterministic
scripted providers are substituted, the system stays fully functional in text mode, and
no vendor SDK is ever called. This is what CI uses.

## Safety boundaries

The system distinguishes three categories and treats them differently:

1. **Retrieval of existing medical information** — allowed after verification. Dosage text
   is returned exactly as stored; the LLM may not paraphrase it into new instructions.
2. **Administrative workflows** — allowed. Refill *requests* are created and marked pending
   clinician review; the system never authorises a refill.
3. **New medical or clinical advice** — never. Diagnosis, treatment, dose changes, and
   side-effect triage are refused and escalated with a structured handoff.

Read [SAFETY.md](SAFETY.md) before extending any workflow.

## FHIR

FHIR R4 throughout, against HAPI locally. Resources in use: `Patient`, `Practitioner`,
`PractitionerRole`, `Location`, `Schedule`, `Slot`, `Appointment`, `Medication`,
`MedicationRequest`, `Condition`, `AllergyIntolerance`, `Encounter`. Operation → resource
mapping and example requests: **[FHIR.md](FHIR.md)**.

## Documentation

| Doc | |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | System design, all three input paths, layering rules |
| [ROADMAP.md](ROADMAP.md) | 18 phases with acceptance criteria |
| [SAFETY.md](SAFETY.md) | Allowed vs prohibited, escalation triggers |
| [FHIR.md](FHIR.md) | Resources, mappings, example requests |
| [AGENTS.md](AGENTS.md) | Orchestrator vs workflow vs tool vs service vs policy |
| [COSTS.md](COSTS.md) | Budget, metering, guard behaviour |
| [DEMO.md](DEMO.md) | Reproducible demo scripts |
| [PROJECT_STATUS.md](PROJECT_STATUS.md) | Living status |
| [docs/](docs/) | Deep dives + [ADRs](docs/decisions/) |

## Demo

Screenshots and a recorded walkthrough land here once the dashboard exists (Phase 10).
See [DEMO.md](DEMO.md) for the scripted scenarios, including the medication-safety
escalation.

## Licence

MIT. Synthetic data only.
