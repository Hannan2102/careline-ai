# FHIR

FHIR **R4** throughout. Primary server: **HAPI FHIR** (Docker, PostgreSQL-backed).
A `MemoryFHIRProvider` mirrors the same resource shapes in-process for machines without
Docker and for the test suite.

## Resources in use

| Resource | Role |
|---|---|
| `Patient` | Demographics, identity verification target |
| `Practitioner` | Dr. Patel, Dr. Johnson, Dr. Chen |
| `PractitionerRole` | Specialty, clinic affiliation, availability context |
| `Location` | Oakwood Family Medicine |
| `Schedule` | Per-practitioner bookable calendar |
| `Slot` | Bookable time windows; `free` / `busy` |
| `Appointment` | Booked visits; `booked` / `cancelled` / `fulfilled` |
| `Medication` | Drug reference |
| `MedicationRequest` | **Source of truth for dosage instructions** |
| `Condition` | Diabetes, hypertension on demo patients |
| `AllergyIntolerance` | Allergy list |
| `Encounter` | Past visit history |
| `HealthcareService` | Optional; service catalogue if it earns its place |
| `ServiceRequest` | Candidate carrier for refill requests (see below) |

## Operation → resource mapping

| System operation | FHIR interaction |
|---|---|
| `search_patient(name, dob)` | `GET /Patient?family=..&given=..&birthdate=..` |
| `get_patient(ref)` | `GET /Patient/{id}` |
| `get_appointments(patient)` | `GET /Appointment?patient=Patient/{id}&status=booked&_sort=date` |
| `get_available_slots(...)` | `GET /Slot?schedule=Schedule/{id}&status=free&start=ge..&start=le..` |
| `create_appointment(...)` | `POST /Appointment` + `PUT /Slot/{id}` → `busy` |
| `cancel_appointment(...)` | `PATCH /Appointment/{id}` → `cancelled` + `PUT /Slot/{id}` → `free` |
| `reschedule_appointment(...)` | cancel + book, old slot freed, new slot busied |
| `get_medications(patient)` | `GET /MedicationRequest?patient=Patient/{id}&status=active` |
| `get_medication_request(patient, name)` | filtered client-side on medication display |
| `create_refill_request(...)` | application table `refill_request` (see below) |
| `get_practitioners()` | `GET /PractitionerRole?_include=PractitionerRole:practitioner` |

### Where refill requests live

A refill request is a **workflow artifact awaiting clinician review**, not a clinical
order. Writing it into the EHR as a `MedicationRequest` would misrepresent it as a
prescription — exactly the confusion [SAFETY.md](SAFETY.md) forbids. So it is persisted in
the application schema with a reference to the source `MedicationRequest`, and it never
becomes a FHIR order. `ServiceRequest` is documented as the natural mapping if a real
integration required one; it is not used by default.

## Domain models, not FHIR types

FHIR resources stop at the adapter. Services, tools, workflows, and the agent see plain
Pydantic domain models (`Patient`, `Appointment`, `AvailableSlot`, `MedicationSummary`,
`Practitioner`). Mapping lives in `backend/app/fhir/mappings.py`.

Reasons: swapping HAPI for Epic must not ripple upward; the agent should never be able to
reach an unmapped field; and dosage text gets a single, auditable extraction point.

**Dosage extraction** — `MedicationRequest.dosageInstruction[0].text` is copied verbatim
into `MedicationSummary.dosage_instruction`. If that field is absent, the mapping records
`None` rather than assembling a sentence from the structured components. The agent then
says it cannot find the instruction and offers escalation. Constructing a dosage from
parts is a clinical act and is out of bounds.

## Example requests

```http
### Find a patient
GET /fhir/Patient?family=Smith&given=John&birthdate=1985-02-15
Accept: application/fhir+json

### Free 30-minute slots for Dr. Patel next week
GET /fhir/Slot?schedule=Schedule/sched-patel&status=free&start=ge2026-09-14&start=le2026-09-18

### Book
POST /fhir/Appointment
Content-Type: application/fhir+json

{
  "resourceType": "Appointment",
  "status": "booked",
  "appointmentType": {
    "coding": [{"system": "http://oakwood.example/appointment-type",
                "code": "DIABETES_FOLLOW_UP", "display": "Diabetes follow-up"}]
  },
  "start": "2026-09-15T14:00:00Z",
  "end":   "2026-09-15T14:30:00Z",
  "minutesDuration": 30,
  "slot": [{"reference": "Slot/slot-patel-20260915-1400"}],
  "participant": [
    {"actor": {"reference": "Patient/demo-john-smith"}, "status": "accepted"},
    {"actor": {"reference": "Practitioner/prac-sarah-patel"}, "status": "accepted"},
    {"actor": {"reference": "Location/loc-oakwood"}, "status": "accepted"}
  ],
  "reasonCode": [{"text": "Diabetes follow-up"}]
}

### Active medications
GET /fhir/MedicationRequest?patient=Patient/demo-john-smith&status=active
```

A `MedicationRequest` as stored for the demo patient:

```json
{
  "resourceType": "MedicationRequest",
  "id": "medreq-john-metformin",
  "status": "active",
  "intent": "order",
  "subject": {"reference": "Patient/demo-john-smith"},
  "requester": {"reference": "Practitioner/prac-sarah-patel"},
  "medicationCodeableConcept": {
    "coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                "code": "860975", "display": "Metformin hydrochloride 500 MG"}],
    "text": "Metformin 500 mg"
  },
  "dosageInstruction": [{"text": "One tablet twice daily with meals"}],
  "dispenseRequest": {"numberOfRepeatsAllowed": 3}
}
```

`"One tablet twice daily with meals"` is the exact string the agent reads back.

## Synthetic data

- **Curated** demo patients, committed as FHIR JSON, with predictable identifiers so demos
  and tests are reproducible.
- **Bulk** patients (50–100) generated with [Synthea](https://github.com/synthetichealth/synthea)
  and loaded as transaction bundles. Synthea output is generated, not committed.

See [synthetic-data/README.md](synthetic-data/README.md).

## HAPI configuration

`hapijpa` image, FHIR R4, PostgreSQL persistence, `hapi.fhir.allow_external_references`
enabled for the curated relative references, validation left permissive for a demo.
Full config in `docker-compose.yml` and `infra/hapi/`.

## Epic (Phase 16)

`EpicFHIRProvider` will target the Epic on FHIR developer sandbox using SMART on FHIR
backend-services OAuth (JWT client assertion → bearer token). Differences to absorb in the
adapter, not upward: Epic's search-parameter subset, `Slot`/`Schedule` availability model,
write restrictions, and its own resource identifiers. Selecting it must remain a
one-line config change — that requirement is what justified the abstraction
([ADR 001](docs/decisions/001-fhir-abstraction.md)).
