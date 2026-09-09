"""The EHRProvider contract.

Every assertion here runs twice -- against the in-memory provider and against a
real HAPI FHIR server -- from one body of test code. Two implementations that
pass the same assertions is the only evidence that swapping them is safe, which
is the whole premise of ADR 001.

The HAPI runs are marked ``integration`` by the fixture and skip when no server
is reachable, so the default suite stays offline and free.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest
from tests.conftest import JOHN_SMITH, JOHN_SMITH_DOB, SEED_TODAY

from app.ehr.base import EHRConflictError, EHRNotFoundError, EHRProvider
from app.schemas.domain import AppointmentStatus, AppointmentType

SEARCH_FROM = SEED_TODAY + timedelta(days=1)
SEARCH_TO = SEED_TODAY + timedelta(days=14)


async def first_free_slot(
    ehr: EHRProvider,
    appointment_type: AppointmentType = AppointmentType.FOLLOW_UP,
    practitioner_ref: str | None = None,
):
    slots = await ehr.get_available_slots(
        appointment_type, SEARCH_FROM, SEARCH_TO, practitioner_ref=practitioner_ref, limit=5
    )
    assert slots, "expected the seeded clinic to have availability"
    return slots[0]


class TestPatientSearch:
    async def test_exact_match_returns_one_patient(self, ehr: EHRProvider) -> None:
        matches = await ehr.search_patients("John Smith", JOHN_SMITH_DOB)
        assert [p.reference for p in matches] == [JOHN_SMITH]

    async def test_unknown_patient_returns_nothing(self, ehr: EHRProvider) -> None:
        assert await ehr.search_patients("Jane Doe", date(1970, 1, 1)) == []

    async def test_wrong_dob_does_not_match(self, ehr: EHRProvider) -> None:
        assert await ehr.search_patients("John Smith", date(1985, 2, 16)) == []

    async def test_duplicate_name_and_dob_returns_both_candidates(self, ehr: EHRProvider) -> None:
        """Ambiguity is surfaced, not resolved here -- the caller asks for a second factor."""
        matches = await ehr.search_patients("Robert Johnson", date(1990, 6, 21))
        assert len(matches) == 2
        assert len({p.phone_last_four for p in matches}) == 2

    async def test_search_is_case_and_whitespace_insensitive(self, ehr: EHRProvider) -> None:
        matches = await ehr.search_patients("  john   SMITH ", JOHN_SMITH_DOB)
        assert len(matches) == 1


class TestBooking:
    async def test_booking_creates_an_appointment_and_consumes_the_slot(
        self, ehr: EHRProvider
    ) -> None:
        slot = await first_free_slot(ehr, AppointmentType.DIABETES_FOLLOW_UP)
        appointment = await ehr.book_appointment(
            JOHN_SMITH, slot.slot_id, AppointmentType.DIABETES_FOLLOW_UP, "Diabetes follow-up"
        )

        assert appointment.status is AppointmentStatus.BOOKED
        assert appointment.patient_ref == JOHN_SMITH
        assert (appointment.end - appointment.start) == timedelta(minutes=30)
        # 30 minutes occupies two 15-minute grid slots.
        assert len(appointment.slot_ids) == 2

        still_free = await ehr.get_available_slots(
            AppointmentType.DIABETES_FOLLOW_UP,
            SEARCH_FROM,
            SEARCH_TO,
            practitioner_ref=slot.practitioner_ref,
            limit=50,
        )
        assert slot.start not in [s.start for s in still_free]

    async def test_appointment_type_determines_duration(self, ehr: EHRProvider) -> None:
        slot = await first_free_slot(ehr, AppointmentType.NEW_PATIENT)
        appointment = await ehr.book_appointment(
            JOHN_SMITH, slot.slot_id, AppointmentType.NEW_PATIENT, "First visit"
        )
        assert (appointment.end - appointment.start) == timedelta(minutes=45)
        assert len(appointment.slot_ids) == 3

    async def test_double_booking_the_same_slot_is_refused(self, ehr: EHRProvider) -> None:
        slot = await first_free_slot(ehr)
        other = await ehr.create_patient("Alex", "Rivera", date(1991, 5, 5))
        await ehr.book_appointment(JOHN_SMITH, slot.slot_id, AppointmentType.FOLLOW_UP)

        with pytest.raises(EHRConflictError, match="no longer available"):
            await ehr.book_appointment(other.reference, slot.slot_id, AppointmentType.FOLLOW_UP)

    async def test_concurrent_booking_of_one_slot_has_exactly_one_winner(
        self, ehr: EHRProvider
    ) -> None:
        """Correctness is enforced at the data layer, not by conversational politeness."""
        slot = await first_free_slot(ehr)
        rivals = [await ehr.create_patient("Racer", f"One{n}", date(1990, 1, 1)) for n in range(5)]
        results = await asyncio.gather(
            *(
                ehr.book_appointment(p.reference, slot.slot_id, AppointmentType.FOLLOW_UP)
                for p in rivals
            ),
            return_exceptions=True,
        )
        winners = [r for r in results if not isinstance(r, BaseException)]
        losers = [r for r in results if isinstance(r, EHRConflictError)]
        assert len(winners) == 1
        assert len(losers) == 4

    async def test_patient_cannot_hold_two_overlapping_appointments(self, ehr: EHRProvider) -> None:
        slots = await ehr.get_available_slots(
            AppointmentType.FOLLOW_UP, SEARCH_FROM, SEARCH_TO, limit=10
        )
        # Same start time, two different practitioners.
        first, second = (
            slots[0],
            next(
                s
                for s in slots
                if s.start == slots[0].start and s.practitioner_ref != slots[0].practitioner_ref
            ),
        )
        await ehr.book_appointment(JOHN_SMITH, first.slot_id, AppointmentType.FOLLOW_UP)
        with pytest.raises(EHRConflictError, match="already has an appointment"):
            await ehr.book_appointment(JOHN_SMITH, second.slot_id, AppointmentType.FOLLOW_UP)

    async def test_booking_an_unknown_slot_raises(self, ehr: EHRProvider) -> None:
        with pytest.raises(EHRNotFoundError, match="unknown slot"):
            await ehr.book_appointment(
                JOHN_SMITH, "slot-nope-19990101-0900", AppointmentType.FOLLOW_UP
            )

    async def test_booking_an_unknown_patient_raises(self, ehr: EHRProvider) -> None:
        slot = await first_free_slot(ehr)
        with pytest.raises(EHRNotFoundError, match="unknown patient"):
            await ehr.book_appointment(
                "Patient/does-not-exist", slot.slot_id, AppointmentType.FOLLOW_UP
            )


class TestCancellation:
    async def test_cancelling_releases_the_slot(self, ehr: EHRProvider) -> None:
        slot = await first_free_slot(ehr, AppointmentType.SICK_VISIT)
        appointment = await ehr.book_appointment(
            JOHN_SMITH, slot.slot_id, AppointmentType.SICK_VISIT
        )

        cancelled = await ehr.cancel_appointment(appointment.appointment_id)
        assert cancelled.status is AppointmentStatus.CANCELLED

        free_again = await ehr.get_available_slots(
            AppointmentType.SICK_VISIT,
            SEARCH_FROM,
            SEARCH_TO,
            practitioner_ref=slot.practitioner_ref,
            limit=50,
        )
        assert slot.start in [s.start for s in free_again]

    async def test_cancelled_appointments_leave_the_booked_list(self, ehr: EHRProvider) -> None:
        booked = await ehr.get_appointments(JOHN_SMITH)
        assert len(booked) == 1  # the seeded demo appointment
        await ehr.cancel_appointment(booked[0].appointment_id)
        assert await ehr.get_appointments(JOHN_SMITH) == []

    async def test_cancelling_twice_is_idempotent(self, ehr: EHRProvider) -> None:
        booked = await ehr.get_appointments(JOHN_SMITH)
        first = await ehr.cancel_appointment(booked[0].appointment_id)
        second = await ehr.cancel_appointment(booked[0].appointment_id)
        assert first.status is second.status is AppointmentStatus.CANCELLED

    async def test_cancelling_an_unknown_appointment_raises(self, ehr: EHRProvider) -> None:
        with pytest.raises(EHRNotFoundError):
            await ehr.cancel_appointment("appt-nope")


class TestRescheduling:
    async def test_rescheduling_frees_the_old_slot_and_books_the_new_one(
        self, ehr: EHRProvider
    ) -> None:
        original = (await ehr.get_appointments(JOHN_SMITH))[0]
        candidates = await ehr.get_available_slots(
            original.appointment_type, SEARCH_FROM, SEARCH_TO, limit=1000
        )
        later = [s for s in candidates if s.start.date() > original.start.date()]
        assert later, "expected availability on a later day"
        target = later[0]

        moved = await ehr.reschedule_appointment(original.appointment_id, target.slot_id)

        assert moved.start == target.start
        assert moved.appointment_type is original.appointment_type
        assert moved.reason == original.reason

        booked = await ehr.get_appointments(JOHN_SMITH)
        assert [a.appointment_id for a in booked] == [moved.appointment_id]

        # The original time is bookable again.
        free_now = await ehr.get_available_slots(
            original.appointment_type,
            SEARCH_FROM,
            SEARCH_TO,
            practitioner_ref=original.practitioner_ref,
            limit=100,
        )
        assert original.start in [s.start for s in free_now]

    async def test_failed_reschedule_leaves_the_original_intact(self, ehr: EHRProvider) -> None:
        """A move that cannot complete must not lose the appointment the patient had."""
        original = (await ehr.get_appointments(JOHN_SMITH))[0]

        with pytest.raises(EHRNotFoundError):
            await ehr.reschedule_appointment(original.appointment_id, "slot-nope-19990101-0900")

        unchanged = (await ehr.get_appointments(JOHN_SMITH))[0]
        assert unchanged.appointment_id == original.appointment_id
        assert unchanged.start == original.start
        assert unchanged.status is AppointmentStatus.BOOKED

    async def test_cancelled_appointments_cannot_be_rescheduled(self, ehr: EHRProvider) -> None:
        original = (await ehr.get_appointments(JOHN_SMITH))[0]
        await ehr.cancel_appointment(original.appointment_id)
        slot = await first_free_slot(ehr)
        with pytest.raises(EHRConflictError, match="not booked"):
            await ehr.reschedule_appointment(original.appointment_id, slot.slot_id)


class TestAvailability:
    async def test_no_slots_are_offered_at_weekends(self, ehr: EHRProvider) -> None:
        slots = await ehr.get_available_slots(
            AppointmentType.FOLLOW_UP, SEARCH_FROM, SEARCH_TO, limit=500
        )
        assert slots
        assert all(s.start.weekday() < 5 for s in slots)

    async def test_offered_slots_carry_the_requested_duration(self, ehr: EHRProvider) -> None:
        slots = await ehr.get_available_slots(
            AppointmentType.ANNUAL_PHYSICAL, SEARCH_FROM, SEARCH_TO, limit=10
        )
        assert all(s.duration_minutes == 45 for s in slots)

    async def test_filtering_by_practitioner_returns_only_that_practitioner(
        self, ehr: EHRProvider
    ) -> None:
        ref = "Practitioner/prac-sarah-patel"
        slots = await ehr.get_available_slots(
            AppointmentType.FOLLOW_UP, SEARCH_FROM, SEARCH_TO, practitioner_ref=ref, limit=20
        )
        assert slots
        assert {s.practitioner_ref for s in slots} == {ref}
        assert all(s.practitioner_name == "Dr. Sarah Patel" for s in slots)

    async def test_unknown_practitioner_raises(self, ehr: EHRProvider) -> None:
        with pytest.raises(EHRNotFoundError, match="unknown practitioner"):
            await ehr.get_available_slots(
                AppointmentType.FOLLOW_UP,
                SEARCH_FROM,
                SEARCH_TO,
                practitioner_ref="Practitioner/prac-nobody",
            )

    async def test_the_seeded_demo_appointment_blocks_its_own_time(self, ehr: EHRProvider) -> None:
        booked = (await ehr.get_appointments(JOHN_SMITH))[0]
        slots = await ehr.get_available_slots(
            AppointmentType.FOLLOW_UP,
            SEARCH_FROM,
            SEARCH_TO,
            practitioner_ref=booked.practitioner_ref,
            limit=500,
        )
        assert booked.start not in [s.start for s in slots]


class TestMedications:
    async def test_active_medications_are_returned_with_verbatim_dosage(
        self, ehr: EHRProvider
    ) -> None:
        medications = await ehr.get_medications(JOHN_SMITH)
        by_name = {m.display_name: m for m in medications}
        assert by_name["Metformin 500 mg"].dosage_instruction == "One tablet twice daily with meals"
        assert by_name["Lisinopril 10 mg"].dosage_instruction == "One tablet once daily"

    async def test_medication_lookup_by_name_is_case_insensitive(self, ehr: EHRProvider) -> None:
        found = await ehr.get_medication_request(JOHN_SMITH, "metformin")
        assert found is not None
        assert found.display_name == "Metformin 500 mg"

    async def test_unknown_medication_returns_none(self, ehr: EHRProvider) -> None:
        assert await ehr.get_medication_request(JOHN_SMITH, "amoxicillin") is None

    async def test_a_prescription_without_dosage_text_reports_none(self, ehr: EHRProvider) -> None:
        """The deliberate fixture: the agent must escalate, not invent a dosage."""
        found = await ehr.get_medication_request("Patient/demo-linda-nguyen", "atorvastatin")
        assert found is not None
        assert found.dosage_instruction is None
        assert found.has_dosage_on_file is False

    async def test_medications_are_scoped_to_the_patient(self, ehr: EHRProvider) -> None:
        maria = await ehr.get_medications("Patient/demo-maria-garcia")
        assert {m.display_name for m in maria} == {"Albuterol inhaler"}


class TestClinicalRecords:
    async def test_conditions_and_allergies_are_returned(self, ehr: EHRProvider) -> None:
        conditions = await ehr.get_conditions(JOHN_SMITH)
        assert {c.display_name for c in conditions} == {"Type 2 Diabetes", "Hypertension"}
        allergies = await ehr.get_allergies(JOHN_SMITH)
        assert [a.substance for a in allergies] == ["Penicillin"]


class TestPatientCreation:
    async def test_created_patients_are_searchable(self, ehr: EHRProvider) -> None:
        created = await ehr.create_patient(
            "Nina", "Okafor", date(1994, 7, 19), phone="(555) 019-0999", postal_code="45042"
        )
        assert created.is_synthetic is True
        found = await ehr.search_patients("Nina Okafor", date(1994, 7, 19))
        assert [p.reference for p in found] == [created.reference]

    async def test_new_patients_can_book_immediately(self, ehr: EHRProvider) -> None:
        created = await ehr.create_patient("Sam", "Doyle", date(2000, 2, 2))
        slot = await first_free_slot(ehr, AppointmentType.NEW_PATIENT)
        appointment = await ehr.book_appointment(
            created.reference, slot.slot_id, AppointmentType.NEW_PATIENT, "First visit"
        )
        assert appointment.patient_ref == created.reference
