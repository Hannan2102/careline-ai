"""Clinic configuration and deterministic FAQ lookup."""

from __future__ import annotations

from app.config.clinic import (
    PRACTITIONERS,
    find_practitioner_by_name,
    lookup_faq,
    schedule_ref_for,
)
from app.schemas.domain import AppointmentType


def test_the_clinic_has_three_named_providers() -> None:
    names = {p.display_name for p in PRACTITIONERS}
    assert names == {"Dr. Sarah Patel", "Dr. Michael Johnson", "Dr. Emily Chen"}


def test_appointment_types_carry_the_specified_durations() -> None:
    assert AppointmentType.NEW_PATIENT.duration_minutes == 45
    assert AppointmentType.FOLLOW_UP.duration_minutes == 20
    assert AppointmentType.SICK_VISIT.duration_minutes == 30
    assert AppointmentType.ANNUAL_PHYSICAL.duration_minutes == 45
    assert AppointmentType.DIABETES_FOLLOW_UP.duration_minutes == 30
    assert AppointmentType.HYPERTENSION_FOLLOW_UP.duration_minutes == 20
    assert AppointmentType.MEDICATION_FOLLOW_UP.duration_minutes == 20


def test_every_appointment_type_has_a_duration() -> None:
    assert all(t.duration_minutes > 0 for t in AppointmentType)


class TestPractitionerResolution:
    def test_surname_resolves(self) -> None:
        found = find_practitioner_by_name("Dr. Patel")
        assert found is not None and found.reference == "Practitioner/prac-sarah-patel"

    def test_full_name_resolves(self) -> None:
        found = find_practitioner_by_name("emily chen")
        assert found is not None and found.specialty == "Family Medicine"

    def test_unknown_name_resolves_to_nothing(self) -> None:
        assert find_practitioner_by_name("Dr. Nobody") is None

    def test_empty_name_resolves_to_nothing(self) -> None:
        assert find_practitioner_by_name("   ") is None


class TestFaq:
    def test_weekend_question_is_answered_from_structured_data(self) -> None:
        answer = lookup_faq("saturday")
        assert answer is not None
        assert "closed on Saturday and Sunday" in answer

    def test_aliases_map_to_one_canonical_answer(self) -> None:
        assert lookup_faq("open") == lookup_faq("hours") == lookup_faq("Opening Hours")

    def test_unknown_topic_returns_none_so_the_caller_escalates(self) -> None:
        """We would rather escalate than improvise a clinic fact."""
        assert lookup_faq("do you validate parking for the mall") is None
        assert lookup_faq("mri scheduling") is None

    def test_insurance_answer_is_clearly_a_demo_list(self) -> None:
        answer = lookup_faq("insurance")
        assert answer is not None and "(demo)" in answer


def test_schedule_reference_derives_from_the_practitioner() -> None:
    assert schedule_ref_for("Practitioner/prac-sarah-patel") == "Schedule/sched-sarah-patel"
