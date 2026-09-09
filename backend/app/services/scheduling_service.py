"""Appointment scheduling.

Business rules the EHR adapter deliberately does not own:

* classifying a stated reason into an appointment type, which fixes duration
* choosing *which* free times to offer, rather than dumping the first N
* enforcing that an appointment belongs to the patient before changing it

The last one matters most. ``EHRProvider.cancel_appointment`` takes only an
appointment id -- it has no notion of who is asking. Ownership is checked here,
so no caller can cancel or move a stranger's appointment by guessing an id.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from pydantic import BaseModel, ConfigDict

from app.config.clinic import PRACTITIONERS_BY_REF
from app.ehr.base import (
    EHRConflictError,
    EHRError,
    EHRNotFoundError,
    EHRProvider,
    EHRUnavailableError,
)
from app.observability.logging import get_logger
from app.schemas.domain import Appointment, AppointmentStatus, AppointmentType, AvailableSlot
from app.services.base import (
    ConflictError,
    NotFoundError,
    NotOwnedError,
    UpstreamUnavailableError,
    ValidationError,
)

logger = get_logger(__name__)

#: Nobody can attend an appointment starting in five minutes.
MIN_BOOKING_LEAD = timedelta(hours=1)

#: How far ahead a search may reach before it stops being useful.
MAX_SEARCH_WINDOW_DAYS = 90

#: Offering two or three options is a decision; offering twelve is a menu.
DEFAULT_OFFER_COUNT = 3


class AppointmentClassification(BaseModel):
    """The visit type inferred from a stated reason."""

    model_config = ConfigDict(frozen=True)

    appointment_type: AppointmentType
    matched_term: str | None
    is_fallback: bool

    @property
    def duration_minutes(self) -> int:
        return self.appointment_type.duration_minutes


#: Reason keywords in priority order: specific conditions before generic visits,
#: so "diabetes check-up" is a diabetes follow-up rather than a routine one.
#: This is the deterministic baseline. An LLM may propose a type, but only from
#: this enum, and this table is the fallback when it cannot (docs/call-flows.md).
REASON_KEYWORDS: tuple[tuple[AppointmentType, tuple[str, ...]], ...] = (
    (
        AppointmentType.NEW_PATIENT,
        (
            "new patient",
            "first visit",
            "first appointment",
            "never been",
            "new to the clinic",
            "register",
        ),
    ),
    (
        AppointmentType.DIABETES_FOLLOW_UP,
        ("diabetes", "diabetic", "a1c", "blood sugar", "metformin review"),
    ),
    (
        AppointmentType.HYPERTENSION_FOLLOW_UP,
        ("hypertension", "blood pressure", "bp check", "lisinopril review"),
    ),
    (
        AppointmentType.MEDICATION_FOLLOW_UP,
        (
            "medication review",
            "medication check",
            "prescription review",
            "med review",
            "review my medication",
        ),
    ),
    (
        AppointmentType.ANNUAL_PHYSICAL,
        ("annual", "physical", "yearly", "wellness", "check up", "checkup"),
    ),
    (
        AppointmentType.SICK_VISIT,
        (
            "sick",
            "cold",
            "flu",
            "fever",
            "cough",
            "sore throat",
            "infection",
            "rash",
            "stomach",
            "unwell",
            "not feeling well",
        ),
    ),
    (AppointmentType.FOLLOW_UP, ("follow up", "follow-up", "check in", "results")),
)


def classify_reason(reason: str | None) -> AppointmentClassification:
    """Map a stated reason to an appointment type.

    Falls back to ``FOLLOW_UP`` rather than guessing, and flags the fallback so
    the visit note can say the reason was not recognised.
    """
    text = (reason or "").lower().strip()
    if text:
        for appointment_type, keywords in REASON_KEYWORDS:
            for keyword in keywords:
                if keyword in text:
                    return AppointmentClassification(
                        appointment_type=appointment_type, matched_term=keyword, is_fallback=False
                    )
    return AppointmentClassification(
        appointment_type=AppointmentType.FOLLOW_UP, matched_term=None, is_fallback=True
    )


def spread_offers(
    slots: list[AvailableSlot], count: int = DEFAULT_OFFER_COUNT
) -> list[AvailableSlot]:
    """Pick a varied handful of times to offer.

    The raw availability list is dense -- the first three entries are usually
    09:00, 09:15 and 09:30 with the same clinician on the same morning, which
    is one option presented three times. Preferring a different day or
    clinician for each offer gives the patient a real choice, then tops up from
    what is left if the schedule is too thin to vary.
    """
    if count <= 0 or not slots:
        return []

    chosen: list[AvailableSlot] = []
    seen: set[tuple[date, str]] = set()
    for slot in slots:
        key = (slot.start.date(), slot.practitioner_ref)
        if key not in seen:
            seen.add(key)
            chosen.append(slot)
            if len(chosen) == count:
                return chosen

    for slot in slots:
        if slot not in chosen:
            chosen.append(slot)
            if len(chosen) == count:
                break
    return chosen


class SchedulingService:
    """Appointment search, booking, and changes."""

    def __init__(self, ehr: EHRProvider) -> None:
        self.ehr = ehr

    # ------------------------------------------------------------- reading
    async def get_upcoming_appointments(
        self, patient_ref: str, now: datetime | None = None
    ) -> list[Appointment]:
        """Booked appointments still in the future, earliest first."""
        moment = now or datetime.now(UTC)
        return [a for a in await self._booked(patient_ref) if a.start > moment]

    async def find_offers(
        self,
        appointment_type: AppointmentType,
        start_date: date,
        end_date: date,
        practitioner_ref: str | None = None,
        count: int = DEFAULT_OFFER_COUNT,
        now: datetime | None = None,
    ) -> list[AvailableSlot]:
        """A small, varied set of bookable times.

        Filters out anything too soon to attend, then spreads the offers.
        """
        moment = now or datetime.now(UTC)
        if end_date < start_date:
            raise ValidationError("end date cannot precede start date")
        if (end_date - start_date).days > MAX_SEARCH_WINDOW_DAYS:
            raise ValidationError(f"search window cannot exceed {MAX_SEARCH_WINDOW_DAYS} days")
        if practitioner_ref and practitioner_ref not in PRACTITIONERS_BY_REF:
            raise NotFoundError(f"no practitioner {practitioner_ref!r} at this clinic")

        try:
            slots = await self.ehr.get_available_slots(
                appointment_type=appointment_type,
                start_date=start_date,
                end_date=end_date,
                practitioner_ref=practitioner_ref,
                limit=200,
            )
        except EHRNotFoundError as exc:
            raise NotFoundError(str(exc)) from exc
        except (EHRUnavailableError, EHRError) as exc:
            raise UpstreamUnavailableError(str(exc)) from exc

        bookable = [s for s in slots if s.start - moment >= MIN_BOOKING_LEAD]
        return spread_offers(bookable, count)

    # ------------------------------------------------------------- writing
    async def book(
        self,
        patient_ref: str,
        slot_id: str,
        appointment_type: AppointmentType,
        reason: str | None = None,
    ) -> Appointment:
        """Book a slot for a patient."""
        try:
            appointment = await self.ehr.book_appointment(
                patient_ref=patient_ref,
                slot_id=slot_id,
                appointment_type=appointment_type,
                reason=reason,
            )
        except EHRConflictError as exc:
            # Expected and recoverable: the workflow re-offers other times.
            logger.info("booking_conflict", patient_ref=patient_ref, slot_id=slot_id)
            raise ConflictError(str(exc)) from exc
        except EHRNotFoundError as exc:
            raise NotFoundError(str(exc)) from exc
        except EHRError as exc:
            raise UpstreamUnavailableError(str(exc)) from exc

        logger.info(
            "appointment_booked",
            patient_ref=patient_ref,
            appointment_id=appointment.appointment_id,
            appointment_type=appointment.appointment_type.value,
        )
        return appointment

    async def cancel(self, patient_ref: str, appointment_id: str) -> Appointment:
        """Cancel an appointment the patient owns, releasing its slots."""
        await self._assert_owned(patient_ref, appointment_id)
        try:
            appointment = await self.ehr.cancel_appointment(appointment_id)
        except EHRNotFoundError as exc:
            raise NotFoundError(str(exc)) from exc
        except EHRError as exc:
            raise UpstreamUnavailableError(str(exc)) from exc

        logger.info("appointment_cancelled", patient_ref=patient_ref, appointment_id=appointment_id)
        return appointment

    async def reschedule(
        self, patient_ref: str, appointment_id: str, new_slot_id: str
    ) -> Appointment:
        """Move an appointment the patient owns.

        The original booking survives a failed move -- the EHR adapter restores
        the released slots and re-raises.
        """
        await self._assert_owned(patient_ref, appointment_id)
        try:
            appointment = await self.ehr.reschedule_appointment(appointment_id, new_slot_id)
        except EHRConflictError as exc:
            logger.info(
                "reschedule_conflict", patient_ref=patient_ref, appointment_id=appointment_id
            )
            raise ConflictError(str(exc)) from exc
        except EHRNotFoundError as exc:
            raise NotFoundError(str(exc)) from exc
        except EHRError as exc:
            raise UpstreamUnavailableError(str(exc)) from exc

        logger.info(
            "appointment_rescheduled",
            patient_ref=patient_ref,
            previous_appointment_id=appointment_id,
            appointment_id=appointment.appointment_id,
        )
        return appointment

    # ----------------------------------------------------------- internals
    async def _booked(self, patient_ref: str) -> list[Appointment]:
        try:
            return await self.ehr.get_appointments(
                patient_ref, statuses=(AppointmentStatus.BOOKED,)
            )
        except EHRError as exc:
            raise UpstreamUnavailableError(str(exc)) from exc

    async def _assert_owned(self, patient_ref: str, appointment_id: str) -> Appointment:
        """Confirm the appointment belongs to this patient.

        Raises ``NotOwnedError`` when it exists but belongs to someone else, so
        the audit trail records the attempt. The caller must not reveal that
        distinction to the patient.
        """
        for appointment in await self._booked(patient_ref):
            if appointment.appointment_id == appointment_id:
                return appointment

        logger.warning(
            "appointment_ownership_denied",
            patient_ref=patient_ref,
            appointment_id=appointment_id,
        )
        raise NotOwnedError(
            f"appointment {appointment_id!r} is not a booked appointment for {patient_ref!r}"
        )
