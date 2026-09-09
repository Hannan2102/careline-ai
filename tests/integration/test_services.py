"""Domain services over the EHR interface (Phase 2).

Runs against both providers via the parametrized ``ehr`` fixture, so a service
rule is proved against the in-memory store and against real HAPI alike.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from tests.conftest import JOHN_SMITH, JOHN_SMITH_DOB, SEED_TODAY

from app.ehr.base import EHRProvider
from app.schemas.domain import AppointmentStatus, AppointmentType
from app.services.base import (
    ConflictError,
    NotFoundError,
    NotOwnedError,
    ValidationError,
)
from app.services.medication_service import (
    MedicationLookupStatus,
    MedicationService,
)
from app.services.patient_service import PatientService
from app.services.scheduling_service import SchedulingService

SEARCH_FROM = SEED_TODAY + timedelta(days=1)
SEARCH_TO = SEED_TODAY + timedelta(days=14)
#: Before the seeded window, so MIN_BOOKING_LEAD never filters everything out.
NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


class TestPatientService:
    async def test_exact_match_returns_the_patient(self, ehr: EHRProvider) -> None:
        found = await PatientService(ehr).find_candidates("John Smith", JOHN_SMITH_DOB)
        assert [p.reference for p in found] == [JOHN_SMITH]

    async def test_unknown_patient_yields_no_candidates(self, ehr: EHRProvider) -> None:
        assert await PatientService(ehr).find_candidates("Jane Doe", date(1970, 1, 1)) == []

    async def test_ambiguity_is_surfaced_not_resolved(self, ehr: EHRProvider) -> None:
        """Two records share this name and DOB; the service must return both."""
        found = await PatientService(ehr).find_candidates("Robert Johnson", date(1990, 6, 21))
        assert len(found) == 2

    async def test_a_single_name_is_rejected(self, ehr: EHRProvider) -> None:
        with pytest.raises(ValidationError, match="full name"):
            await PatientService(ehr).find_candidates("John", JOHN_SMITH_DOB)

    async def test_future_date_of_birth_is_rejected(self, ehr: EHRProvider) -> None:
        tomorrow = datetime.now(UTC).date() + timedelta(days=1)
        with pytest.raises(ValidationError, match="future"):
            await PatientService(ehr).find_candidates("John Smith", tomorrow)

    async def test_implausible_date_of_birth_is_rejected(self, ehr: EHRProvider) -> None:
        with pytest.raises(ValidationError, match="implausible"):
            await PatientService(ehr).find_candidates("John Smith", date(1080, 1, 1))

    async def test_missing_patient_raises_not_found(self, ehr: EHRProvider) -> None:
        with pytest.raises(NotFoundError):
            await PatientService(ehr).get_patient("Patient/does-not-exist")

    async def test_registration_creates_a_findable_record(self, ehr: EHRProvider) -> None:
        service = PatientService(ehr)
        created = await service.register_new_patient(
            "  Nina ", " Okafor ", date(1994, 7, 19), phone="(555) 019-0999"
        )
        assert (created.given_name, created.family_name) == ("Nina", "Okafor")
        found = await service.find_candidates("Nina Okafor", date(1994, 7, 19))
        assert [p.reference for p in found] == [created.reference]

    async def test_registration_rejects_incomplete_details(self, ehr: EHRProvider) -> None:
        service = PatientService(ehr)
        with pytest.raises(ValidationError, match="given name"):
            await service.register_new_patient("", "Okafor", date(1994, 7, 19))
        with pytest.raises(ValidationError, match="phone"):
            await service.register_new_patient("Nina", "Okafor", date(1994, 7, 19), phone="12")
        with pytest.raises(ValidationError, match="email"):
            await service.register_new_patient(
                "Nina", "Okafor", date(1994, 7, 19), email="not-an-email"
            )


class TestSchedulingService:
    async def test_offers_are_few_and_varied(self, ehr: EHRProvider) -> None:
        offers = await SchedulingService(ehr).find_offers(
            AppointmentType.DIABETES_FOLLOW_UP, SEARCH_FROM, SEARCH_TO, now=NOW
        )
        assert 1 <= len(offers) <= 3
        assert len({(o.start.date(), o.practitioner_ref) for o in offers}) == len(offers)

    async def test_offers_respect_the_requested_duration(self, ehr: EHRProvider) -> None:
        offers = await SchedulingService(ehr).find_offers(
            AppointmentType.ANNUAL_PHYSICAL, SEARCH_FROM, SEARCH_TO, now=NOW
        )
        assert all(o.duration_minutes == 45 for o in offers)

    async def test_slots_too_soon_to_attend_are_not_offered(self, ehr: EHRProvider) -> None:
        """Nobody can make an appointment starting in five minutes."""
        service = SchedulingService(ehr)
        all_offers = await service.find_offers(
            AppointmentType.FOLLOW_UP, SEARCH_FROM, SEARCH_TO, count=1, now=NOW
        )
        assert all_offers
        just_before = all_offers[0].start - timedelta(minutes=5)
        later = await service.find_offers(
            AppointmentType.FOLLOW_UP, SEARCH_FROM, SEARCH_TO, count=1, now=just_before
        )
        assert not later or later[0].start > all_offers[0].start

    async def test_inverted_date_range_is_rejected(self, ehr: EHRProvider) -> None:
        with pytest.raises(ValidationError, match="precede"):
            await SchedulingService(ehr).find_offers(
                AppointmentType.FOLLOW_UP, SEARCH_TO, SEARCH_FROM, now=NOW
            )

    async def test_absurd_search_window_is_rejected(self, ehr: EHRProvider) -> None:
        with pytest.raises(ValidationError, match="exceed"):
            await SchedulingService(ehr).find_offers(
                AppointmentType.FOLLOW_UP,
                SEARCH_FROM,
                SEARCH_FROM + timedelta(days=400),
                now=NOW,
            )

    async def test_unknown_practitioner_is_rejected(self, ehr: EHRProvider) -> None:
        with pytest.raises(NotFoundError, match="practitioner"):
            await SchedulingService(ehr).find_offers(
                AppointmentType.FOLLOW_UP,
                SEARCH_FROM,
                SEARCH_TO,
                practitioner_ref="Practitioner/prac-nobody",
                now=NOW,
            )

    async def test_booking_then_listing_shows_the_appointment(self, ehr: EHRProvider) -> None:
        service = SchedulingService(ehr)
        offers = await service.find_offers(
            AppointmentType.SICK_VISIT, SEARCH_FROM, SEARCH_TO, now=NOW
        )
        booked = await service.book(
            JOHN_SMITH, offers[0].slot_id, AppointmentType.SICK_VISIT, "sore throat"
        )
        upcoming = await service.get_upcoming_appointments(JOHN_SMITH, now=NOW)
        assert booked.appointment_id in [a.appointment_id for a in upcoming]

    async def test_past_appointments_are_not_listed_as_upcoming(self, ehr: EHRProvider) -> None:
        service = SchedulingService(ehr)
        existing = await service.get_upcoming_appointments(JOHN_SMITH, now=NOW)
        assert existing
        far_future = existing[0].start + timedelta(days=365)
        assert await service.get_upcoming_appointments(JOHN_SMITH, now=far_future) == []

    async def test_taken_slot_raises_a_recoverable_conflict(self, ehr: EHRProvider) -> None:
        service = SchedulingService(ehr)
        other = await PatientService(ehr).register_new_patient("Alex", "Rivera", date(1991, 5, 5))
        offers = await service.find_offers(
            AppointmentType.FOLLOW_UP, SEARCH_FROM, SEARCH_TO, now=NOW
        )
        await service.book(other.reference, offers[0].slot_id, AppointmentType.FOLLOW_UP)
        with pytest.raises(ConflictError):
            await service.book(JOHN_SMITH, offers[0].slot_id, AppointmentType.FOLLOW_UP)


class TestAppointmentOwnership:
    """The EHR adapter cancels by id alone; the service decides who may.

    Without this check, anyone could cancel a stranger's appointment by
    guessing an identifier.
    """

    async def _someone_elses_appointment(self, ehr: EHRProvider) -> tuple[str, str]:
        """Book an appointment for another patient. Returns (patient_ref, id)."""
        service = SchedulingService(ehr)
        other = await PatientService(ehr).register_new_patient("Priya", "Raman", date(1987, 3, 3))
        offers = await service.find_offers(
            AppointmentType.FOLLOW_UP, SEARCH_FROM, SEARCH_TO, now=NOW
        )
        booked = await service.book(other.reference, offers[0].slot_id, AppointmentType.FOLLOW_UP)
        return other.reference, booked.appointment_id

    async def test_cannot_cancel_another_patients_appointment(self, ehr: EHRProvider) -> None:
        _, appointment_id = await self._someone_elses_appointment(ehr)
        with pytest.raises(NotOwnedError):
            await SchedulingService(ehr).cancel(JOHN_SMITH, appointment_id)

    async def test_cannot_reschedule_another_patients_appointment(self, ehr: EHRProvider) -> None:
        service = SchedulingService(ehr)
        _, appointment_id = await self._someone_elses_appointment(ehr)
        offers = await service.find_offers(
            AppointmentType.FOLLOW_UP, SEARCH_FROM, SEARCH_TO, count=3, now=NOW
        )
        with pytest.raises(NotOwnedError):
            await service.reschedule(JOHN_SMITH, appointment_id, offers[-1].slot_id)

    async def test_a_refused_cancellation_leaves_the_appointment_booked(
        self, ehr: EHRProvider
    ) -> None:
        """Refusing must not be a partial cancel: the owner keeps their slot."""
        service = SchedulingService(ehr)
        owner_ref, appointment_id = await self._someone_elses_appointment(ehr)

        with pytest.raises(NotOwnedError):
            await service.cancel(JOHN_SMITH, appointment_id)

        owners_appointments = await service.get_upcoming_appointments(owner_ref, now=NOW)
        assert [a.appointment_id for a in owners_appointments] == [appointment_id]
        assert owners_appointments[0].status is AppointmentStatus.BOOKED

    async def test_owner_can_cancel_and_reschedule(self, ehr: EHRProvider) -> None:
        service = SchedulingService(ehr)
        mine = (await service.get_upcoming_appointments(JOHN_SMITH, now=NOW))[0]
        offers = await service.find_offers(
            mine.appointment_type, SEARCH_FROM, SEARCH_TO, count=3, now=NOW
        )
        moved = await service.reschedule(JOHN_SMITH, mine.appointment_id, offers[-1].slot_id)
        assert moved.appointment_id != mine.appointment_id

        cancelled = await service.cancel(JOHN_SMITH, moved.appointment_id)
        assert cancelled.status.value == "cancelled"
        assert await service.get_upcoming_appointments(JOHN_SMITH, now=NOW) == []

    async def test_unknown_appointment_is_refused(self, ehr: EHRProvider) -> None:
        with pytest.raises(NotOwnedError):
            await SchedulingService(ehr).cancel(JOHN_SMITH, "appt-does-not-exist")


class TestMedicationService:
    async def test_active_medications_are_listed(self, ehr: EHRProvider) -> None:
        medications = await MedicationService(ehr).list_active(JOHN_SMITH)
        assert {m.display_name for m in medications} == {
            "Metformin 500 mg",
            "Lisinopril 10 mg",
        }

    async def test_found_medication_carries_the_stored_instruction(self, ehr: EHRProvider) -> None:
        result = await MedicationService(ehr).look_up(JOHN_SMITH, "metformin")
        assert result.status is MedicationLookupStatus.FOUND
        assert result.medication is not None
        assert result.medication.dosage_instruction == "One tablet twice daily with meals"

    async def test_unknown_medication_is_reported_as_not_found(self, ehr: EHRProvider) -> None:
        result = await MedicationService(ehr).look_up(JOHN_SMITH, "amoxicillin")
        assert result.status is MedicationLookupStatus.NOT_FOUND
        assert result.is_answerable is False

    async def test_prescription_without_dosage_is_distinct_from_missing(
        self, ehr: EHRProvider
    ) -> None:
        """These need different responses, so they must not collapse to one."""
        result = await MedicationService(ehr).look_up("Patient/demo-linda-nguyen", "atorvastatin")
        assert result.status is MedicationLookupStatus.NO_DOSAGE_ON_FILE
        assert result.is_answerable is False
        assert result.medication is not None

    async def test_empty_medication_name_is_rejected(self, ehr: EHRProvider) -> None:
        with pytest.raises(ValidationError, match="medication name"):
            await MedicationService(ehr).look_up(JOHN_SMITH, "   ")

    async def test_medications_do_not_leak_across_patients(self, ehr: EHRProvider) -> None:
        result = await MedicationService(ehr).look_up("Patient/demo-maria-garcia", "metformin")
        assert result.status is MedicationLookupStatus.NOT_FOUND

    async def test_rendered_dosage_quotes_the_record_verbatim(self, ehr: EHRProvider) -> None:
        service = MedicationService(ehr)
        result = await service.look_up(JOHN_SMITH, "metformin")
        assert result.medication is not None
        sentence = service.describe_dosage(result.medication)

        assert "One tablet twice daily with meals" in sentence
        assert "prescription on file" in sentence
        assert service.dosage_is_verbatim(sentence, result.medication) is True

    async def test_the_verbatim_guard_rejects_altered_wording(self, ehr: EHRProvider) -> None:
        """The model may word the sentence; it may not word the dosage."""
        service = MedicationService(ehr)
        result = await service.look_up(JOHN_SMITH, "metformin")
        assert result.medication is not None
        for tampered in [
            "Take two tablets twice daily with meals.",
            "One tablet twice a day with meals.",
            "Your prescription says metformin, one tablet twice daily with meals.",
        ]:
            assert service.dosage_is_verbatim(tampered, result.medication) is False

    async def test_describing_an_absent_dosage_is_refused(self, ehr: EHRProvider) -> None:
        service = MedicationService(ehr)
        result = await service.look_up("Patient/demo-linda-nguyen", "atorvastatin")
        assert result.medication is not None
        with pytest.raises(ValidationError, match="no dosage on file"):
            service.describe_dosage(result.medication)
