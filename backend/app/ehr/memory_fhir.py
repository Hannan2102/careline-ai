"""In-process FHIR store implementing :class:`EHRProvider`.

Exists so the whole system -- and the whole test suite -- runs with no Docker
and no network (ADR 001). It stores genuine FHIR-shaped resources and converts
them through the same ``fhir/mappings.py`` used for HAPI, so the mapping layer
is exercised here too and the two providers cannot quietly diverge.

It is a development and test convenience, not a second source of truth: the
seed script produces identical resources for both providers.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta

from app.config.clinic import PRACTITIONERS, PRACTITIONERS_BY_REF, schedule_ref_for
from app.ehr.base import (
    EHRConflictError,
    EHRNotFoundError,
    EHRProvider,
)
from app.fhir.mappings import (
    allergy_to_domain,
    appointment_to_domain,
    appointment_to_fhir,
    condition_to_domain,
    medication_request_to_domain,
    new_patient_reference,
    patient_to_domain,
    patient_to_fhir,
)
from app.fhir.models import FhirResource, get_path
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
from app.utils.scheduling import (
    covering_grid_starts,
    date_range,
    fits_within_working_hours,
    grid_starts,
    intervals_overlap,
    slot_id_for,
    to_utc,
)


def practitioner_ref_from_schedule(schedule_ref: str) -> str:
    """'Schedule/sched-sarah-patel' -> 'Practitioner/prac-sarah-patel'."""
    return "Practitioner/prac-" + schedule_ref.rsplit("sched-", 1)[-1]


class InMemoryFhirStore:
    """A dictionary of FHIR resources keyed by type and id."""

    def __init__(self) -> None:
        self._resources: dict[str, dict[str, FhirResource]] = {}

    def put(self, resource: FhirResource) -> None:
        self._resources.setdefault(resource["resourceType"], {})[resource["id"]] = resource

    def put_all(self, resources: list[FhirResource]) -> None:
        for resource in resources:
            self.put(resource)

    def get(self, resource_type: str, resource_id: str) -> FhirResource | None:
        return self._resources.get(resource_type, {}).get(resource_id)

    def all_of(self, resource_type: str) -> list[FhirResource]:
        return list(self._resources.get(resource_type, {}).values())

    def clear(self) -> None:
        self._resources.clear()

    def count(self, resource_type: str) -> int:
        return len(self._resources.get(resource_type, {}))

    @property
    def summary(self) -> dict[str, int]:
        return {rt: len(items) for rt, items in sorted(self._resources.items())}


class MemoryFHIRProvider(EHRProvider):
    """``EHRProvider`` backed by :class:`InMemoryFhirStore`."""

    name = "memory"

    def __init__(self, store: InMemoryFhirStore | None = None) -> None:
        self.store = store if store is not None else InMemoryFhirStore()
        # Serialises the read-check-write sequences. Booking is the one
        # genuinely contended operation, and "exactly one wins" is a
        # correctness requirement, not a nicety.
        self._write_lock = asyncio.Lock()

    # ----------------------------------------------------------- patients
    async def search_patients(self, full_name: str, date_of_birth: date) -> list[Patient]:
        wanted = " ".join(full_name.lower().split())
        matches: list[Patient] = []
        for resource in self.store.all_of("Patient"):
            if resource.get("birthDate") != date_of_birth.isoformat():
                continue
            patient = patient_to_domain(resource)
            if patient.full_name.lower() == wanted:
                matches.append(patient)
        return matches

    async def get_patient(self, patient_ref: str) -> Patient | None:
        resource = self.store.get("Patient", patient_ref.rsplit("/", 1)[-1])
        return patient_to_domain(resource) if resource else None

    async def create_patient(
        self,
        given_name: str,
        family_name: str,
        date_of_birth: date,
        phone: str | None = None,
        email: str | None = None,
        postal_code: str | None = None,
    ) -> Patient:
        patient = Patient(
            reference=new_patient_reference(given_name, family_name),
            given_name=given_name,
            family_name=family_name,
            date_of_birth=date_of_birth,
            phone=phone,
            email=email,
            postal_code=postal_code,
        )
        self.store.put(patient_to_fhir(patient))
        return patient

    # ------------------------------------------------------ practitioners
    async def get_practitioners(self) -> list[Practitioner]:
        return list(PRACTITIONERS)

    # ------------------------------------------------------- appointments
    async def get_appointments(
        self,
        patient_ref: str,
        statuses: tuple[AppointmentStatus, ...] = (AppointmentStatus.BOOKED,),
    ) -> list[Appointment]:
        wanted = {s.value for s in statuses}
        appointments = [
            appointment_to_domain(resource)
            for resource in self.store.all_of("Appointment")
            if resource.get("status") in wanted
            and any(
                get_path(p, "actor", "reference") == patient_ref
                for p in resource.get("participant", [])
            )
        ]
        return sorted(appointments, key=lambda a: a.start)

    async def get_available_slots(
        self,
        appointment_type: AppointmentType,
        start_date: date,
        end_date: date,
        practitioner_ref: str | None = None,
        limit: int = 20,
    ) -> list[AvailableSlot]:
        duration = appointment_type.duration_minutes
        practitioners = (
            [PRACTITIONERS_BY_REF[practitioner_ref]]
            if practitioner_ref and practitioner_ref in PRACTITIONERS_BY_REF
            else list(PRACTITIONERS)
        )
        if practitioner_ref and practitioner_ref not in PRACTITIONERS_BY_REF:
            raise EHRNotFoundError(f"unknown practitioner {practitioner_ref!r}")

        found: list[AvailableSlot] = []
        for day in date_range(start_date, end_date):
            for start in grid_starts(day):
                if not fits_within_working_hours(start, duration):
                    continue
                for practitioner in practitioners:
                    if self._slots_free(practitioner.reference, start, duration):
                        found.append(
                            AvailableSlot(
                                slot_id=slot_id_for(practitioner.reference, start),
                                practitioner_ref=practitioner.reference,
                                practitioner_name=practitioner.display_name,
                                start=start,
                                end=start + timedelta(minutes=duration),
                            )
                        )
        found.sort(key=lambda s: (s.start, s.practitioner_name))
        return found[:limit]

    async def book_appointment(
        self,
        patient_ref: str,
        slot_id: str,
        appointment_type: AppointmentType,
        reason: str | None = None,
    ) -> Appointment:
        async with self._write_lock:
            return self._book_locked(patient_ref, slot_id, appointment_type, reason)

    async def cancel_appointment(self, appointment_id: str) -> Appointment:
        async with self._write_lock:
            return self._cancel_locked(appointment_id)

    async def reschedule_appointment(self, appointment_id: str, new_slot_id: str) -> Appointment:
        async with self._write_lock:
            existing = self.store.get("Appointment", appointment_id)
            if existing is None:
                raise EHRNotFoundError(f"unknown appointment {appointment_id!r}")
            if existing.get("status") != AppointmentStatus.BOOKED.value:
                raise EHRConflictError(
                    f"appointment {appointment_id!r} is {existing.get('status')}, not booked"
                )

            old = appointment_to_domain(existing)
            released = self._set_slot_status(list(old.slot_ids), "free")
            try:
                booked = self._book_locked(
                    old.patient_ref, new_slot_id, old.appointment_type, old.reason
                )
            except Exception:
                # Leave the original booking untouched if the new slot cannot
                # be taken (docs/call-flows.md).
                self._set_slot_status(released, "busy")
                raise
            self._cancel_locked(appointment_id, release_slots=False)
            return booked

    # -------------------------------------------------------- medications
    async def get_medications(self, patient_ref: str) -> list[MedicationSummary]:
        return [
            medication_request_to_domain(resource)
            for resource in self.store.all_of("MedicationRequest")
            if get_path(resource, "subject", "reference") == patient_ref
            and resource.get("status") == "active"
        ]

    async def get_medication_request(
        self, patient_ref: str, medication_name: str
    ) -> MedicationSummary | None:
        needle = medication_name.strip().lower()
        if not needle:
            return None
        matches = [
            medication
            for medication in await self.get_medications(patient_ref)
            if needle in medication.display_name.lower()
        ]
        # Ambiguity is not resolved here -- the workflow asks which one.
        return matches[0] if len(matches) == 1 else None

    # ------------------------------------------------------ clinical read
    async def get_conditions(self, patient_ref: str) -> list[Condition]:
        return [
            condition_to_domain(resource)
            for resource in self.store.all_of("Condition")
            if get_path(resource, "subject", "reference") == patient_ref
        ]

    async def get_allergies(self, patient_ref: str) -> list[Allergy]:
        return [
            allergy_to_domain(resource)
            for resource in self.store.all_of("AllergyIntolerance")
            if get_path(resource, "patient", "reference") == patient_ref
        ]

    # ------------------------------------------------------------ internals
    def _slots_free(self, practitioner_ref: str, start: datetime, duration: int) -> bool:
        """True when every grid slot the appointment would consume exists and is free."""
        for grid_start in covering_grid_starts(start, duration):
            resource = self.store.get("Slot", slot_id_for(practitioner_ref, grid_start))
            if resource is None or resource.get("status") != "free":
                return False
        return True

    def _set_slot_status(self, slot_ids: list[str], status: str) -> list[str]:
        changed: list[str] = []
        for slot_id in slot_ids:
            resource = self.store.get("Slot", slot_id)
            if resource is not None:
                resource["status"] = status
                changed.append(slot_id)
        return changed

    def _book_locked(
        self,
        patient_ref: str,
        slot_id: str,
        appointment_type: AppointmentType,
        reason: str | None,
    ) -> Appointment:
        slot = self.store.get("Slot", slot_id)
        if slot is None:
            raise EHRNotFoundError(f"unknown slot {slot_id!r}")
        if self.store.get("Patient", patient_ref.rsplit("/", 1)[-1]) is None:
            raise EHRNotFoundError(f"unknown patient {patient_ref!r}")
        if slot.get("status") != "free":
            raise EHRConflictError(f"slot {slot_id!r} is no longer available")

        practitioner_ref = practitioner_ref_from_schedule(
            get_path(slot, "schedule", "reference", default="")
        )
        if practitioner_ref not in PRACTITIONERS_BY_REF:
            raise EHRNotFoundError(f"slot {slot_id!r} has no resolvable practitioner")

        start = to_utc(datetime.fromisoformat(slot["start"].replace("Z", "+00:00")))
        duration = appointment_type.duration_minutes
        end = start + timedelta(minutes=duration)

        if not fits_within_working_hours(start, duration):
            raise EHRConflictError(
                f"a {duration}-minute {appointment_type.display} does not fit at "
                f"{start.isoformat()} within clinic hours"
            )
        if not self._slots_free(practitioner_ref, start, duration):
            raise EHRConflictError(
                f"slot {slot_id!r} cannot accommodate {duration} minutes without overlapping "
                "another appointment"
            )

        # A patient may not hold two appointments at once, even with different
        # practitioners.
        for resource in self.store.all_of("Appointment"):
            if resource.get("status") != AppointmentStatus.BOOKED.value:
                continue
            if not any(
                get_path(p, "actor", "reference") == patient_ref
                for p in resource.get("participant", [])
            ):
                continue
            other = appointment_to_domain(resource)
            if intervals_overlap(start, end, other.start, other.end):
                raise EHRConflictError(
                    f"patient already has an appointment at {other.start.isoformat()}"
                )

        consumed = [
            slot_id_for(practitioner_ref, grid_start)
            for grid_start in covering_grid_starts(start, duration)
        ]
        self._set_slot_status(consumed, "busy")

        resource = appointment_to_fhir(
            appointment_id=f"appt-{uuid.uuid4().hex[:12]}",
            patient_ref=patient_ref,
            practitioner_ref=practitioner_ref,
            appointment_type=appointment_type,
            start=start,
            end=end,
            slot_ids=consumed,
            reason=reason,
        )
        self.store.put(resource)
        return appointment_to_domain(resource)

    def _cancel_locked(self, appointment_id: str, release_slots: bool = True) -> Appointment:
        resource = self.store.get("Appointment", appointment_id)
        if resource is None:
            raise EHRNotFoundError(f"unknown appointment {appointment_id!r}")
        if resource.get("status") == AppointmentStatus.CANCELLED.value:
            return appointment_to_domain(resource)

        resource["status"] = AppointmentStatus.CANCELLED.value
        if release_slots:
            self._set_slot_status(
                [str(s["reference"]).rsplit("/", 1)[-1] for s in resource.get("slot", [])],
                "free",
            )
        self.store.put(resource)
        return appointment_to_domain(resource)


def build_slot_resources(days: list[date]) -> list[FhirResource]:
    """Free Slot resources for every practitioner across the given days."""
    from app.fhir.mappings import slot_to_fhir

    resources: list[FhirResource] = []
    for practitioner in PRACTITIONERS:
        schedule_ref = schedule_ref_for(practitioner.reference)
        for day in days:
            for start in grid_starts(day):
                resources.append(
                    slot_to_fhir(
                        slot_id=slot_id_for(practitioner.reference, start),
                        schedule_ref=schedule_ref,
                        start=start,
                        end=start + timedelta(minutes=15),
                    )
                )
    return resources


def utc_today() -> date:
    return datetime.now(UTC).date()
