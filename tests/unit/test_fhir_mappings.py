"""FHIR -> domain mapping, including the dosage rule from SAFETY.md."""

from __future__ import annotations

from datetime import date

import pytest

from app.ehr.base import EHRMappingError
from app.fhir.mappings import (
    appointment_to_domain,
    medication_request_to_domain,
    patient_to_domain,
)
from app.fhir.models import FhirResource, get_path
from app.schemas.domain import AppointmentType


def medication_request(**overrides: object) -> FhirResource:
    resource: FhirResource = {
        "resourceType": "MedicationRequest",
        "id": "mr-1",
        "status": "active",
        "subject": {"reference": "Patient/p1"},
        "requester": {"reference": "Practitioner/prac-sarah-patel"},
        "medicationCodeableConcept": {"text": "Metformin 500 mg"},
        "dosageInstruction": [{"text": "One tablet twice daily with meals"}],
        "dispenseRequest": {"numberOfRepeatsAllowed": 3},
    }
    resource.update(overrides)
    return resource


class TestDosageIsVerbatim:
    """The single most safety-critical mapping in the system."""

    def test_dosage_text_is_copied_exactly(self) -> None:
        stored = "One tablet twice daily with meals"
        summary = medication_request_to_domain(
            medication_request(dosageInstruction=[{"text": stored}])
        )
        assert summary.dosage_instruction == stored
        assert summary.has_dosage_on_file is True

    def test_unusual_dosage_text_is_not_normalised(self) -> None:
        """Whitespace and casing are the clinician's, not ours to tidy."""
        stored = "  TAKE 1/2 tablet  q.a.m.  "
        summary = medication_request_to_domain(
            medication_request(dosageInstruction=[{"text": stored}])
        )
        assert summary.dosage_instruction == stored

    def test_missing_dosage_yields_none_rather_than_a_reconstruction(self) -> None:
        """Structured components must never be assembled into an instruction."""
        resource = medication_request()
        del resource["dosageInstruction"]
        resource["dosageInstruction_structured"] = {"timing": "BID", "doseQuantity": 1}
        summary = medication_request_to_domain(resource)
        assert summary.dosage_instruction is None
        assert summary.has_dosage_on_file is False

    def test_empty_dosage_list_is_not_a_dosage(self) -> None:
        summary = medication_request_to_domain(medication_request(dosageInstruction=[]))
        assert summary.dosage_instruction is None


def test_medication_without_a_display_name_raises() -> None:
    resource = medication_request(medicationCodeableConcept={})
    with pytest.raises(EHRMappingError, match="display name"):
        medication_request_to_domain(resource)


def test_prescriber_is_resolved_to_a_display_name() -> None:
    summary = medication_request_to_domain(medication_request())
    assert summary.prescriber_name == "Dr. Sarah Patel"
    assert summary.refills_remaining == 3


def test_patient_prefers_the_official_name() -> None:
    resource: FhirResource = {
        "resourceType": "Patient",
        "id": "p1",
        "name": [
            {"use": "nickname", "family": "Smith", "given": ["Johnny"]},
            {"use": "official", "family": "Smith", "given": ["John"]},
        ],
        "birthDate": "1985-02-15",
        "telecom": [{"system": "phone", "value": "(555) 019-0142"}],
    }
    patient = patient_to_domain(resource)
    assert patient.given_name == "John"
    assert patient.date_of_birth == date(1985, 2, 15)
    assert patient.phone_last_four == "0142"


def test_patient_without_a_name_raises() -> None:
    with pytest.raises(EHRMappingError, match="no name"):
        patient_to_domain({"resourceType": "Patient", "id": "p1", "birthDate": "1985-02-15"})


def test_unknown_appointment_type_is_rejected_not_defaulted() -> None:
    """The wrong type means the wrong duration, so coercion is unsafe."""
    resource: FhirResource = {
        "resourceType": "Appointment",
        "id": "a1",
        "status": "booked",
        "appointmentType": {"coding": [{"code": "WALK_IN_TRIAGE"}]},
        "start": "2026-09-15T14:00:00Z",
        "end": "2026-09-15T14:30:00Z",
        "participant": [
            {"actor": {"reference": "Patient/p1"}},
            {"actor": {"reference": "Practitioner/prac-sarah-patel"}},
        ],
    }
    with pytest.raises(EHRMappingError, match="unknown appointmentType"):
        appointment_to_domain(resource)


def test_appointment_maps_participants_by_role() -> None:
    resource: FhirResource = {
        "resourceType": "Appointment",
        "id": "a1",
        "status": "booked",
        "appointmentType": {"coding": [{"code": AppointmentType.DIABETES_FOLLOW_UP.value}]},
        "start": "2026-09-15T14:00:00Z",
        "end": "2026-09-15T14:30:00Z",
        "slot": [{"reference": "Slot/slot-patel-20260915-1400"}],
        "participant": [
            {"actor": {"reference": "Patient/p1"}},
            {"actor": {"reference": "Practitioner/prac-sarah-patel"}},
            {"actor": {"reference": "Location/loc-oakwood"}},
        ],
        "reasonCode": [{"text": "Diabetes follow-up"}],
    }
    appointment = appointment_to_domain(resource)
    assert appointment.patient_ref == "Patient/p1"
    assert appointment.practitioner_name == "Dr. Sarah Patel"
    assert appointment.location_ref == "Location/loc-oakwood"
    assert appointment.slot_ids == ("slot-patel-20260915-1400",)


def test_get_path_tolerates_absent_optional_elements() -> None:
    """FHIR elements are almost all optional; absence must never raise."""
    assert get_path({"a": {"b": [{"c": 1}]}}, "a", "b", 0, "c") == 1
    assert get_path({"a": {}}, "a", "b", 0, "c") is None
    assert get_path({}, "a", "b", default="fallback") == "fallback"
