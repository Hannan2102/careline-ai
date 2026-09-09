# Agent tools

The complete callable surface exposed to the LLM. Anything not here, the agent cannot do.

## Contract

Every tool has: a Pydantic argument model, a typed result model, an explicit verification
requirement, and an audit record. Tools are thin — they validate, check authorisation,
delegate to a service, and shape the result. Business rules live in services.

## Catalogue

| Tool | Arguments | Returns | Verified? | Mutates |
|---|---|---|---|---|
| `search_patient` | `full_name`, `date_of_birth` | match count + candidate refs (no PHI) | — | no |
| `verify_second_factor` | `session_id`, `factor_type`, `value` | verification result | — | no |
| `get_patient_appointments` | `patient_id` | `list[Appointment]` | ✅ | no |
| `get_available_slots` | `provider_id?`, `appointment_type`, `start_date`, `end_date` | `list[AvailableSlot]` | ✅ | no |
| `book_appointment` | `patient_id`, `slot_id`, `appointment_type`, `reason` | `Appointment` | ✅ | **yes** |
| `cancel_appointment` | `patient_id`, `appointment_id` | `Appointment` | ✅ | **yes** |
| `reschedule_appointment` | `patient_id`, `appointment_id`, `new_slot_id` | `Appointment` | ✅ | **yes** |
| `get_patient_medications` | `patient_id` | `list[MedicationSummary]` | ✅ | no |
| `get_medication_request` | `patient_id`, `medication_name` | `MedicationSummary` | ✅ | no |
| `create_refill_request` | `patient_id`, `medication_request_id` | `RefillRequest` (PENDING_REVIEW) | ✅ | **yes** (app schema) |
| `get_clinic_information` | `topic` | `ClinicInfo` | ❌ | no |
| `create_human_handoff` | `patient_id?`, `reason`, `destination`, `summary`, `priority` | `Escalation` | — | **yes** (app schema) |

`search_patient` returns a **count and opaque candidate references**, never demographics —
it is used to decide whether verification succeeded, and leaking record contents at that
step would defeat the gate entirely.

## Validation

```
LLM tool call
  → Pydantic parse
      ├─ valid   → authorisation check → service → typed result
      └─ invalid → repair prompt (max 2 retries)
                     → still invalid → ask the user, or escalate
```

No coercion, no defaults substituted for missing required fields, no partial execution.
`patient_id` supplied by the model is checked against the session's verified `patient_ref`
and rejected on mismatch — the model cannot address another patient by guessing an id.

## Errors

Tools return typed failures rather than raising into the model: `NOT_FOUND`,
`NOT_VERIFIED`, `CONFLICT` (slot taken), `INVALID_ARGUMENTS`, `UPSTREAM_UNAVAILABLE`,
`POLICY_REFUSED`. Each maps to a defined conversational response. `CONFLICT` re-enters slot
search; `UPSTREAM_UNAVAILABLE` escalates. The agent never narrates a raw exception.

## Adding a tool

Define the schemas, decide the verification requirement, implement the service method,
register the tool, write unit tests for validation and authorisation, and add it to this
table. If a new tool can mutate clinical data, it needs an ADR first.
