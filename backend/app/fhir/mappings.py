"""FHIR R4 <-> domain model mapping.

The one place resource shapes are interpreted. Mapping failures raise
``EHRMappingError`` rather than returning a partially-populated object.

The most important function here is :func:`medication_request_to_domain`: it
copies ``dosageInstruction[0].text`` **verbatim** and records ``None`` when it
is absent, rather than assembling a sentence from the structured components.
Constructing a dosage is a clinical act (SAFETY.md).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from app.config.clinic import CLINIC_LOCATION, PRACTITIONERS_BY_REF
from app.ehr.base import EHRMappingError
from app.fhir.models import FhirResource, get_path, reference
from app.schemas.domain import (
    Allergy,
    Appointment,
    AppointmentStatus,
    AppointmentType,
    AvailableSlot,
    Condition,
    MedicationSummary,
    Patient,
    Practitioner,
)
from app.utils.scheduling import to_utc

APPOINTMENT_TYPE_SYSTEM = "http://oakwood.example/appointment-type"

#: Marks records created at runtime (new-patient registration, tests) as
#: distinct from the curated seed. Everything in this project is synthetic;
#: this identifies the subset a demo reset should clear.
SYNTHETIC_IDENTIFIER_SYSTEM = "http://oakwood.example/synthetic-record"
RUNTIME_GENERATED = "runtime-generated"

#: Id prefix for patients created at runtime. Both providers use it, and a
#: demo reset uses it to tell registered patients from the curated seed.
RUNTIME_PATIENT_ID_PREFIX = "syn-"


def _parse_datetime(value: str | None, *, field: str) -> datetime:
    if not value:
        raise EHRMappingError(f"missing required datetime field '{field}'")
    try:
        return to_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError as exc:  # pragma: no cover - defensive
        raise EHRMappingError(f"unparseable datetime in '{field}': {value!r}") from exc


def _parse_date(value: str | None, *, field: str) -> date:
    if not value:
        raise EHRMappingError(f"missing required date field '{field}'")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise EHRMappingError(f"unparseable date in '{field}': {value!r}") from exc


def _telecom(resource: FhirResource, system: str) -> str | None:
    for entry in resource.get("telecom", []) or []:
        if entry.get("system") == system and entry.get("value"):
            return str(entry["value"])
    return None


# --------------------------------------------------------------------------
# FHIR -> domain
# --------------------------------------------------------------------------


def patient_to_domain(resource: FhirResource) -> Patient:
    names = resource.get("name") or []
    if not names:
        raise EHRMappingError(f"Patient {resource.get('id')!r} has no name element")
    # Prefer the official name when several are present.
    chosen = next((n for n in names if n.get("use") == "official"), names[0])
    given = (chosen.get("given") or [""])[0]
    family = chosen.get("family") or ""
    if not given or not family:
        raise EHRMappingError(f"Patient {resource.get('id')!r} has an incomplete name")

    return Patient(
        reference=reference(resource),
        given_name=given,
        family_name=family,
        date_of_birth=_parse_date(resource.get("birthDate"), field="Patient.birthDate"),
        phone=_telecom(resource, "phone"),
        email=_telecom(resource, "email"),
        postal_code=get_path(resource, "address", 0, "postalCode"),
    )


def practitioner_to_domain(
    resource: FhirResource, specialty: str = "Family Medicine"
) -> Practitioner:
    name = (resource.get("name") or [{}])[0]
    return Practitioner(
        reference=reference(resource),
        given_name=(name.get("given") or [""])[0],
        family_name=name.get("family") or "",
        prefix=(name.get("prefix") or ["Dr."])[0],
        specialty=specialty,
    )


def slot_to_domain(
    resource: FhirResource, practitioner_ref: str, practitioner_name: str
) -> AvailableSlot:
    return AvailableSlot(
        slot_id=resource["id"],
        practitioner_ref=practitioner_ref,
        practitioner_name=practitioner_name,
        start=_parse_datetime(resource.get("start"), field="Slot.start"),
        end=_parse_datetime(resource.get("end"), field="Slot.end"),
    )


def _participant_ref(resource: FhirResource, prefix: str) -> str | None:
    for participant in resource.get("participant", []) or []:
        ref = get_path(participant, "actor", "reference")
        if isinstance(ref, str) and ref.startswith(prefix):
            return ref
    return None


def appointment_to_domain(resource: FhirResource) -> Appointment:
    type_code = get_path(resource, "appointmentType", "coding", 0, "code")
    try:
        appointment_type = AppointmentType(type_code)
    except ValueError as exc:
        # Unknown codes are rejected, never coerced to a default -- the wrong
        # type means the wrong duration.
        raise EHRMappingError(f"unknown appointmentType code {type_code!r}") from exc

    patient_ref = _participant_ref(resource, "Patient/")
    practitioner_ref = _participant_ref(resource, "Practitioner/")
    if not patient_ref or not practitioner_ref:
        raise EHRMappingError(
            f"Appointment {resource.get('id')!r} is missing a patient or practitioner participant"
        )

    practitioner = PRACTITIONERS_BY_REF.get(practitioner_ref)
    return Appointment(
        appointment_id=resource["id"],
        status=AppointmentStatus(resource.get("status", "booked")),
        appointment_type=appointment_type,
        start=_parse_datetime(resource.get("start"), field="Appointment.start"),
        end=_parse_datetime(resource.get("end"), field="Appointment.end"),
        patient_ref=patient_ref,
        practitioner_ref=practitioner_ref,
        practitioner_name=practitioner.display_name if practitioner else practitioner_ref,
        location_ref=_participant_ref(resource, "Location/") or CLINIC_LOCATION.reference,
        reason=get_path(resource, "reasonCode", 0, "text"),
        slot_ids=tuple(
            str(s["reference"]).rsplit("/", 1)[-1]
            for s in resource.get("slot", []) or []
            if isinstance(s, dict) and s.get("reference")
        ),
    )


def medication_request_to_domain(resource: FhirResource) -> MedicationSummary:
    display = get_path(resource, "medicationCodeableConcept", "text") or get_path(
        resource, "medicationCodeableConcept", "coding", 0, "display"
    )
    if not display:
        raise EHRMappingError(
            f"MedicationRequest {resource.get('id')!r} has no medication display name"
        )

    subject = get_path(resource, "subject", "reference")
    if not subject:
        raise EHRMappingError(f"MedicationRequest {resource.get('id')!r} has no subject")

    # Verbatim, or None. Never assembled from structured dosage components.
    dosage_text = get_path(resource, "dosageInstruction", 0, "text")

    prescriber_ref = get_path(resource, "requester", "reference")
    prescriber = PRACTITIONERS_BY_REF.get(prescriber_ref) if prescriber_ref else None

    return MedicationSummary(
        medication_request_id=resource["id"],
        patient_ref=subject,
        display_name=display,
        dosage_instruction=dosage_text,
        status=resource.get("status", "unknown"),
        prescriber_ref=prescriber_ref,
        prescriber_name=prescriber.display_name if prescriber else None,
        refills_remaining=get_path(resource, "dispenseRequest", "numberOfRepeatsAllowed"),
    )


def condition_to_domain(resource: FhirResource) -> Condition:
    display = get_path(resource, "code", "text") or get_path(
        resource, "code", "coding", 0, "display"
    )
    if not display:
        raise EHRMappingError(f"Condition {resource.get('id')!r} has no code display")
    return Condition(
        condition_id=resource["id"],
        patient_ref=get_path(resource, "subject", "reference", default=""),
        display_name=display,
        clinical_status=get_path(resource, "clinicalStatus", "coding", 0, "code", default="active"),
    )


def allergy_to_domain(resource: FhirResource) -> Allergy:
    substance = get_path(resource, "code", "text") or get_path(
        resource, "code", "coding", 0, "display"
    )
    if not substance:
        raise EHRMappingError(f"AllergyIntolerance {resource.get('id')!r} has no substance")
    return Allergy(
        allergy_id=resource["id"],
        patient_ref=get_path(resource, "patient", "reference", default=""),
        substance=substance,
        reaction=get_path(resource, "reaction", 0, "manifestation", 0, "text"),
        criticality=resource.get("criticality"),
    )


# --------------------------------------------------------------------------
# domain -> FHIR
# --------------------------------------------------------------------------


def new_patient_reference(given_name: str, family_name: str) -> str:
    """Reference for a newly registered synthetic patient."""
    slug = f"{given_name}-{family_name}".lower().replace(" ", "-")
    return f"Patient/{RUNTIME_PATIENT_ID_PREFIX}{slug}-{uuid.uuid4().hex[:8]}"


def is_runtime_patient(resource_id: str) -> bool:
    """Whether a Patient id belongs to a runtime-created record."""
    return resource_id.startswith(RUNTIME_PATIENT_ID_PREFIX)


def patient_to_fhir(patient: Patient) -> FhirResource:
    resource: FhirResource = {
        "resourceType": "Patient",
        "id": patient.reference.rsplit("/", 1)[-1],
        "active": True,
        "identifier": [{"system": SYNTHETIC_IDENTIFIER_SYSTEM, "value": RUNTIME_GENERATED}],
        "name": [
            {
                "use": "official",
                "family": patient.family_name,
                "given": [patient.given_name],
            }
        ],
        "birthDate": patient.date_of_birth.isoformat(),
        "telecom": [],
    }
    if patient.phone:
        resource["telecom"].append({"system": "phone", "value": patient.phone, "use": "home"})
    if patient.email:
        resource["telecom"].append({"system": "email", "value": patient.email})
    if patient.postal_code:
        resource["address"] = [{"postalCode": patient.postal_code, "country": "US"}]
    return resource


def slot_to_fhir(
    slot_id: str, schedule_ref: str, start: datetime, end: datetime, status: str = "free"
) -> FhirResource:
    return {
        "resourceType": "Slot",
        "id": slot_id,
        "schedule": {"reference": schedule_ref},
        "status": status,
        "start": start.isoformat().replace("+00:00", "Z"),
        "end": end.isoformat().replace("+00:00", "Z"),
    }


def appointment_to_fhir(
    appointment_id: str,
    patient_ref: str,
    practitioner_ref: str,
    appointment_type: AppointmentType,
    start: datetime,
    end: datetime,
    slot_ids: list[str],
    reason: str | None = None,
    status: AppointmentStatus = AppointmentStatus.BOOKED,
) -> FhirResource:
    resource: FhirResource = {
        "resourceType": "Appointment",
        "id": appointment_id,
        "status": status.value,
        "appointmentType": {
            "coding": [
                {
                    "system": APPOINTMENT_TYPE_SYSTEM,
                    "code": appointment_type.value,
                    "display": appointment_type.display,
                }
            ]
        },
        "start": start.isoformat().replace("+00:00", "Z"),
        "end": end.isoformat().replace("+00:00", "Z"),
        "minutesDuration": appointment_type.duration_minutes,
        "slot": [{"reference": f"Slot/{sid}"} for sid in slot_ids],
        "participant": [
            {"actor": {"reference": patient_ref}, "status": "accepted"},
            {"actor": {"reference": practitioner_ref}, "status": "accepted"},
            {"actor": {"reference": CLINIC_LOCATION.reference}, "status": "accepted"},
        ],
    }
    if reason:
        resource["reasonCode"] = [{"text": reason}]
    return resource
