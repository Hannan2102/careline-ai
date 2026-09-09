# FHIR data model

Resource-level detail behind [../FHIR.md](../FHIR.md).

## Identifier convention

Curated demo resources use stable, readable ids so demos and tests are reproducible:

```
Patient/demo-john-smith
Practitioner/prac-sarah-patel
PractitionerRole/role-patel-family-medicine
Location/loc-oakwood
Schedule/sched-patel
Slot/slot-patel-20260915-1400
MedicationRequest/medreq-john-metformin
```

Synthea-generated bulk patients keep their generated UUIDs. The `demo-` prefix makes
curated records obvious in the dashboard and in logs.

## Slot generation

Slots are generated per practitioner per working day on a 20-minute grid, 08:00–17:00,
Mon–Fri, excluding a 12:00–13:00 break. A 30- or 45-minute appointment consumes
consecutive slots; the scheduling service checks contiguity and marks each consumed slot
`busy`.

This grid is a deliberate simplification. Real availability involves templates, session
types, overbooking rules, and holds. The abstraction point — `get_available_slots` — is
where that complexity would go, and no caller would change.

## Status transitions

| Resource | Transitions used |
|---|---|
| `Slot` | `free` ↔ `busy` |
| `Appointment` | `booked` → `cancelled` \| `fulfilled` \| `noshow` |
| `MedicationRequest` | `active` → `completed` \| `stopped` (read-only for this system) |

The system never transitions a `MedicationRequest`. It only reads them.

## Domain mapping

| FHIR | Domain model | Notes |
|---|---|---|
| `Patient.name[0]` | `Patient.given_name`, `family_name` | Official use preferred |
| `Patient.birthDate` | `Patient.date_of_birth` | `date`, never a string |
| `Patient.telecom[phone]` | `Patient.phone` | Last four used as a second factor |
| `Patient.address[0].postalCode` | `Patient.postal_code` | Alternate second factor |
| `Slot.start/end/status` | `AvailableSlot` | Includes resolved practitioner |
| `Appointment.participant` | `Appointment.patient_ref`, `practitioner_ref`, `location_ref` | Resolved by role |
| `Appointment.appointmentType.coding[0].code` | `AppointmentType` enum | Unknown codes rejected, not coerced |
| `MedicationRequest.medicationCodeableConcept.text` | `MedicationSummary.display_name` | |
| `MedicationRequest.dosageInstruction[0].text` | `MedicationSummary.dosage_instruction` | **Verbatim.** `None` if absent |

## Validation posture

HAPI validation is permissive for a demo, so the adapter validates what actually matters:
required references resolve, appointment types are in the enum, slot times are consistent
with duration, and dosage text is copied without transformation. Mapping failures raise —
they never produce a partially-populated domain object, because a half-empty medication
record is worse than an error.
