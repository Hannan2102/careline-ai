"""The EHR abstraction (ADR 001).

Every read and write of clinical or scheduling data goes through this
interface. Agents, workflows, and tools never see a FHIR resource and never
learn which provider is configured -- swapping HAPI for Epic is a config
change, not a refactor.

Note on refill requests: ``create_refill_request`` is deliberately *not* part
of this interface. A refill request is a workflow artifact awaiting clinician
review, not a clinical order; writing it into the EHR as a MedicationRequest
would misrepresent it as a prescription, which SAFETY.md forbids. It lives in
the application schema instead (see FHIR.md).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

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

# --------------------------------------------------------------------------
# Errors -- mirrored by the tool error taxonomy in docs/agent-tools.md
# --------------------------------------------------------------------------


class EHRError(Exception):
    """Base class for every EHR failure."""


class EHRNotFoundError(EHRError):
    """The requested resource does not exist."""


class EHRConflictError(EHRError):
    """The write lost a race -- typically a slot booked by someone else."""


class EHRUnavailableError(EHRError):
    """The upstream EHR could not be reached or returned an unusable response."""


class EHRMappingError(EHRError):
    """A resource could not be mapped to a domain model.

    Raised rather than returning a partially-populated object: a half-empty
    medication record is worse than an error.
    """


# --------------------------------------------------------------------------
# Interface
# --------------------------------------------------------------------------


class EHRProvider(ABC):
    """Read/write access to patients, scheduling, and medications."""

    #: Short identifier used in logs, health checks, and the dashboard.
    name: str = "abstract"

    # ---------------------------------------------------------- lifecycle
    async def ping(self) -> bool:
        """True when the backing store is reachable. Never raises."""
        return True

    async def aclose(self) -> None:
        """Release any held resources (HTTP clients, pools)."""
        return None

    # ----------------------------------------------------------- patients
    @abstractmethod
    async def search_patients(self, full_name: str, date_of_birth: date) -> list[Patient]:
        """Candidates matching name and date of birth.

        Returns every match. Deciding what a match count *means* -- verified,
        ambiguous, or unknown -- belongs to the verification service, not here
        (ADR 003).
        """

    @abstractmethod
    async def get_patient(self, patient_ref: str) -> Patient | None: ...

    @abstractmethod
    async def create_patient(
        self,
        given_name: str,
        family_name: str,
        date_of_birth: date,
        phone: str | None = None,
        email: str | None = None,
        postal_code: str | None = None,
    ) -> Patient:
        """Create a synthetic patient record (new-patient booking)."""

    # ------------------------------------------------------ practitioners
    @abstractmethod
    async def get_practitioners(self) -> list[Practitioner]: ...

    # ------------------------------------------------------- appointments
    @abstractmethod
    async def get_appointments(
        self,
        patient_ref: str,
        statuses: tuple[AppointmentStatus, ...] = (AppointmentStatus.BOOKED,),
    ) -> list[Appointment]:
        """Appointments for a patient, earliest first."""

    @abstractmethod
    async def get_available_slots(
        self,
        appointment_type: AppointmentType,
        start_date: date,
        end_date: date,
        practitioner_ref: str | None = None,
        limit: int = 20,
    ) -> list[AvailableSlot]:
        """Bookable start times that fit the type's full duration.

        Availability already accounts for working hours, breaks, appointment
        duration, and existing bookings, so a returned slot is genuinely
        bookable at the moment of the query.
        """

    @abstractmethod
    async def book_appointment(
        self,
        patient_ref: str,
        slot_id: str,
        appointment_type: AppointmentType,
        reason: str | None = None,
    ) -> Appointment:
        """Book a slot.

        Raises:
            EHRConflictError: the slot was taken, or the patient already has a
                conflicting appointment.
            EHRNotFoundError: the slot or patient does not exist.
        """

    @abstractmethod
    async def cancel_appointment(self, appointment_id: str) -> Appointment:
        """Cancel and release the slots it held."""

    @abstractmethod
    async def reschedule_appointment(self, appointment_id: str, new_slot_id: str) -> Appointment:
        """Move an appointment.

        The original booking is left untouched if the new slot cannot be taken.
        """

    # -------------------------------------------------------- medications
    @abstractmethod
    async def get_medications(self, patient_ref: str) -> list[MedicationSummary]:
        """Active prescriptions. Dosage text is returned verbatim (SAFETY.md)."""

    @abstractmethod
    async def get_medication_request(
        self, patient_ref: str, medication_name: str
    ) -> MedicationSummary | None:
        """Match one active prescription by name.

        Returns ``None`` when there is no match or the match is ambiguous; the
        caller asks the patient which medication they meant rather than picking.
        """

    # ------------------------------------------------------- clinical read
    @abstractmethod
    async def get_conditions(self, patient_ref: str) -> list[Condition]: ...

    @abstractmethod
    async def get_allergies(self, patient_ref: str) -> list[Allergy]: ...
