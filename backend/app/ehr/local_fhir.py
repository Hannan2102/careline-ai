"""``EHRProvider`` backed by a local HAPI FHIR R4 server.

The primary demo EHR. It shares every scheduling primitive with
:class:`~app.ehr.memory_fhir.MemoryFHIRProvider` (``app/utils/scheduling.py``)
so availability means the same thing in both, and it uses the same mapping
layer so a resource is interpreted identically wherever it came from.

Concurrency is handled with FHIR's own optimistic locking: slot writes are
conditional on ``meta.versionId`` via ``If-Match``, so the loser of a race
gets a 412 and a typed conflict rather than silently overwriting the winner.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

from app.config.clinic import PRACTITIONERS, PRACTITIONERS_BY_REF, schedule_ref_for
from app.ehr.base import (
    EHRConflictError,
    EHRNotFoundError,
    EHRProvider,
)
from app.ehr.memory_fhir import practitioner_ref_from_schedule
from app.fhir.client import FhirClient, version_id
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


class LocalFHIRProvider(EHRProvider):
    """Reads and writes against a HAPI FHIR server over REST."""

    name = "local"

    def __init__(self, client: FhirClient) -> None:
        self.client = client

    async def ping(self) -> bool:
        return await self.client.ping()

    async def aclose(self) -> None:
        await self.client.aclose()

    # ----------------------------------------------------------- patients
    async def search_patients(self, full_name: str, date_of_birth: date) -> list[Patient]:
        parts = full_name.split()
        if len(parts) < 2:
            return []
        given, family = parts[0], parts[-1]
        resources = await self.client.search(
            "Patient",
            {"given": given, "family": family, "birthdate": date_of_birth.isoformat()},
        )
        wanted = " ".join(full_name.lower().split())
        # The server search is a filter, not the decision: confirm the full
        # name exactly so a fuzzy server match cannot widen the gate (ADR 003).
        return [
            patient
            for patient in (patient_to_domain(r) for r in resources)
            if patient.full_name.lower() == wanted
        ]

    async def get_patient(self, patient_ref: str) -> Patient | None:
        resource = await self.client.try_read("Patient", patient_ref.rsplit("/", 1)[-1])
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
        draft = Patient(
            reference=new_patient_reference(given_name, family_name),
            given_name=given_name,
            family_name=family_name,
            date_of_birth=date_of_birth,
            phone=phone,
            email=email,
            postal_code=postal_code,
        )
        created = await self.client.update(patient_to_fhir(draft))
        return patient_to_domain(created)

    # ------------------------------------------------------ practitioners
    async def get_practitioners(self) -> list[Practitioner]:
        # Specialty lives on PractitionerRole; the curated clinic roster is the
        # authority for the demo, and reading it avoids a second round trip.
        return list(PRACTITIONERS)

    # ------------------------------------------------------- appointments
    async def get_appointments(
        self,
        patient_ref: str,
        statuses: tuple[AppointmentStatus, ...] = (AppointmentStatus.BOOKED,),
    ) -> list[Appointment]:
        resources = await self.client.search(
            "Appointment",
            {
                "patient": patient_ref,
                "status": ",".join(s.value for s in statuses),
                "_sort": "date",
                "_count": "50",
            },
        )
        return sorted((appointment_to_domain(r) for r in resources), key=lambda a: a.start)

    async def get_available_slots(
        self,
        appointment_type: AppointmentType,
        start_date: date,
        end_date: date,
        practitioner_ref: str | None = None,
        limit: int = 20,
    ) -> list[AvailableSlot]:
        if practitioner_ref and practitioner_ref not in PRACTITIONERS_BY_REF:
            raise EHRNotFoundError(f"unknown practitioner {practitioner_ref!r}")
        practitioners = (
            [PRACTITIONERS_BY_REF[practitioner_ref]] if practitioner_ref else list(PRACTITIONERS)
        )
        duration = appointment_type.duration_minutes

        free_ids = await self._free_slot_ids(practitioners, start_date, end_date)

        found: list[AvailableSlot] = []
        for day in date_range(start_date, end_date):
            for start in grid_starts(day):
                if not fits_within_working_hours(start, duration):
                    continue
                for practitioner in practitioners:
                    covering = [
                        slot_id_for(practitioner.reference, grid_start)
                        for grid_start in covering_grid_starts(start, duration)
                    ]
                    if all(slot_id in free_ids for slot_id in covering):
                        found.append(
                            AvailableSlot(
                                slot_id=covering[0],
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
        slot = await self.client.try_read("Slot", slot_id)
        if slot is None:
            raise EHRNotFoundError(f"unknown slot {slot_id!r}")
        if await self.get_patient(patient_ref) is None:
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
        await self._assert_no_patient_conflict(patient_ref, start, end)

        consumed = await self._claim_slots(practitioner_ref, start, duration)
        try:
            resource = await self.client.update(
                appointment_to_fhir(
                    appointment_id=f"appt-{uuid.uuid4().hex[:12]}",
                    patient_ref=patient_ref,
                    practitioner_ref=practitioner_ref,
                    appointment_type=appointment_type,
                    start=start,
                    end=end,
                    slot_ids=consumed,
                    reason=reason,
                )
            )
        except Exception:
            await self._release_slots(consumed)
            raise
        return appointment_to_domain(resource)

    async def cancel_appointment(self, appointment_id: str) -> Appointment:
        resource = await self.client.try_read("Appointment", appointment_id)
        if resource is None:
            raise EHRNotFoundError(f"unknown appointment {appointment_id!r}")
        if resource.get("status") == AppointmentStatus.CANCELLED.value:
            return appointment_to_domain(resource)

        resource["status"] = AppointmentStatus.CANCELLED.value
        updated = await self.client.update(resource)
        await self._release_slots(
            [str(s["reference"]).rsplit("/", 1)[-1] for s in resource.get("slot", []) or []]
        )
        return appointment_to_domain(updated)

    async def reschedule_appointment(self, appointment_id: str, new_slot_id: str) -> Appointment:
        resource = await self.client.try_read("Appointment", appointment_id)
        if resource is None:
            raise EHRNotFoundError(f"unknown appointment {appointment_id!r}")
        if resource.get("status") != AppointmentStatus.BOOKED.value:
            raise EHRConflictError(
                f"appointment {appointment_id!r} is {resource.get('status')}, not booked"
            )
        old = appointment_to_domain(resource)

        # Release first so a same-day move can reuse adjacent time, then restore
        # on failure -- the original booking must survive a failed move.
        released = list(old.slot_ids)
        await self._release_slots(released)
        try:
            booked = await self.book_appointment(
                old.patient_ref, new_slot_id, old.appointment_type, old.reason
            )
        except Exception:
            await self._claim_slot_ids(released)
            raise

        resource["status"] = AppointmentStatus.CANCELLED.value
        await self.client.update(resource)
        return booked

    # -------------------------------------------------------- medications
    async def get_medications(self, patient_ref: str) -> list[MedicationSummary]:
        resources = await self.client.search(
            "MedicationRequest", {"patient": patient_ref, "status": "active", "_count": "50"}
        )
        return [medication_request_to_domain(r) for r in resources]

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
        return matches[0] if len(matches) == 1 else None

    # ------------------------------------------------------ clinical read
    async def get_conditions(self, patient_ref: str) -> list[Condition]:
        resources = await self.client.search("Condition", {"patient": patient_ref, "_count": "50"})
        return [condition_to_domain(r) for r in resources]

    async def get_allergies(self, patient_ref: str) -> list[Allergy]:
        resources = await self.client.search(
            "AllergyIntolerance", {"patient": patient_ref, "_count": "50"}
        )
        return [allergy_to_domain(r) for r in resources]

    # ------------------------------------------------------------ internals
    async def _free_slot_ids(
        self, practitioners: list[Practitioner], start_date: date, end_date: date
    ) -> set[str]:
        free: set[str] = set()
        for practitioner in practitioners:
            resources = await self.client.search(
                "Slot",
                {
                    "schedule": schedule_ref_for(practitioner.reference),
                    "status": "free",
                    "start": [f"ge{start_date.isoformat()}", f"le{end_date.isoformat()}T23:59:59Z"],
                    "_count": "500",
                },
            )
            free.update(str(r["id"]) for r in resources)
        return free

    async def _assert_no_patient_conflict(
        self, patient_ref: str, start: datetime, end: datetime
    ) -> None:
        for existing in await self.get_appointments(patient_ref):
            if intervals_overlap(start, end, existing.start, existing.end):
                raise EHRConflictError(
                    f"patient already has an appointment at {existing.start.isoformat()}"
                )

    async def _claim_slots(
        self, practitioner_ref: str, start: datetime, duration: int
    ) -> list[str]:
        """Mark every covered slot busy, conditionally on its current version.

        Rolls back what it already claimed if any slot is lost to a competing
        booking, so a failed attempt leaves no half-held time.
        """
        claimed: list[str] = []
        for grid_start in covering_grid_starts(start, duration):
            slot_id = slot_id_for(practitioner_ref, grid_start)
            resource = await self.client.try_read("Slot", slot_id)
            if resource is None or resource.get("status") != "free":
                await self._release_slots(claimed)
                raise EHRConflictError(f"slot {slot_id!r} is no longer available")
            resource["status"] = "busy"
            try:
                await self.client.update(resource, if_match=version_id(resource))
            except EHRConflictError:
                await self._release_slots(claimed)
                raise
            claimed.append(slot_id)
        return claimed

    async def _claim_slot_ids(self, slot_ids: list[str]) -> None:
        await self._set_status(slot_ids, "busy")

    async def _release_slots(self, slot_ids: list[str]) -> None:
        await self._set_status(slot_ids, "free")

    async def _set_status(self, slot_ids: list[str], status: str) -> None:
        for slot_id in slot_ids:
            resource = await self.client.try_read("Slot", slot_id)
            if resource is None:
                continue
            resource["status"] = status
            await self.client.update(resource)


def build_seed_bundles(resources: list[FhirResource], chunk_size: int = 200) -> list[FhirResource]:
    """Split resources into transaction bundles HAPI will accept in one request."""
    from app.fhir.models import make_bundle

    return [
        make_bundle(resources[i : i + chunk_size]) for i in range(0, len(resources), chunk_size)
    ]
