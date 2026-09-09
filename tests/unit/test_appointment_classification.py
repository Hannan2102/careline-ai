"""Reason classification and slot-offer selection.

Pure functions, so they need no EHR and no event loop.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.schemas.domain import AppointmentType, AvailableSlot
from app.services.scheduling_service import classify_reason, spread_offers


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("I need a diabetes follow-up", AppointmentType.DIABETES_FOLLOW_UP),
        ("checking my blood sugar", AppointmentType.DIABETES_FOLLOW_UP),
        ("blood pressure check", AppointmentType.HYPERTENSION_FOLLOW_UP),
        ("hypertension review", AppointmentType.HYPERTENSION_FOLLOW_UP),
        ("I'd like a medication review", AppointmentType.MEDICATION_FOLLOW_UP),
        ("annual physical please", AppointmentType.ANNUAL_PHYSICAL),
        ("my yearly wellness visit", AppointmentType.ANNUAL_PHYSICAL),
        ("I have a sore throat", AppointmentType.SICK_VISIT),
        ("bad cough and fever", AppointmentType.SICK_VISIT),
        ("I'm a new patient", AppointmentType.NEW_PATIENT),
        ("first visit with your clinic", AppointmentType.NEW_PATIENT),
        ("just a follow up", AppointmentType.FOLLOW_UP),
    ],
)
def test_reasons_map_to_the_expected_type(reason: str, expected: AppointmentType) -> None:
    assert classify_reason(reason).appointment_type is expected


def test_specific_conditions_win_over_generic_visits() -> None:
    """'diabetes check up' is a diabetes follow-up, not an annual physical."""
    result = classify_reason("diabetes check up")
    assert result.appointment_type is AppointmentType.DIABETES_FOLLOW_UP


@pytest.mark.parametrize("reason", ["", "   ", None, "something entirely unrelated"])
def test_unrecognised_reasons_fall_back_and_say_so(reason: str | None) -> None:
    """Fallback is flagged so the visit note can record that it was not understood."""
    result = classify_reason(reason)
    assert result.appointment_type is AppointmentType.FOLLOW_UP
    assert result.is_fallback is True
    assert result.matched_term is None


def test_classification_carries_the_duration() -> None:
    assert classify_reason("annual physical").duration_minutes == 45
    assert classify_reason("sore throat").duration_minutes == 30


def _slot(day: int, hour: int, practitioner: str) -> AvailableSlot:
    start = datetime(2026, 9, 14, hour, tzinfo=UTC) + timedelta(days=day)
    return AvailableSlot(
        slot_id=f"slot-{practitioner}-{day}-{hour}",
        practitioner_ref=f"Practitioner/prac-{practitioner}",
        practitioner_name=f"Dr. {practitioner.title()}",
        start=start,
        end=start + timedelta(minutes=20),
    )


class TestSpreadOffers:
    def test_offers_vary_by_day_or_clinician(self) -> None:
        """Three consecutive slots on one morning is one option, not three."""
        dense = [
            _slot(0, 9, "patel"),
            _slot(0, 10, "patel"),
            _slot(0, 11, "patel"),
            _slot(1, 9, "patel"),
            _slot(0, 9, "chen"),
        ]
        offers = spread_offers(dense, count=3)
        assert len(offers) == 3
        assert len({(o.start.date(), o.practitioner_ref) for o in offers}) == 3

    def test_thin_schedules_are_topped_up_rather_than_left_short(self) -> None:
        """Better to offer two times on one morning than only one."""
        only_one_morning = [_slot(0, 9, "patel"), _slot(0, 10, "patel")]
        offers = spread_offers(only_one_morning, count=3)
        assert len(offers) == 2

    def test_earliest_option_is_offered_first(self) -> None:
        slots = [_slot(0, 9, "patel"), _slot(1, 9, "chen"), _slot(2, 9, "johnson")]
        assert spread_offers(slots, count=3)[0] is slots[0]

    def test_no_availability_yields_no_offers(self) -> None:
        assert spread_offers([], count=3) == []

    def test_requesting_zero_offers_returns_none(self) -> None:
        assert spread_offers([_slot(0, 9, "patel")], count=0) == []
