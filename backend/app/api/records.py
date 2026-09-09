"""Patients, appointments, and practitioners, read from the EHR.

These are the dashboard's window onto the record. They read through the same
``EHRProvider`` the agent uses, so the dashboard cannot disagree with the agent
about what is in the chart.

Every patient here is synthetic, and the responses say so in a field rather
than relying on the UI to remember -- ``synthetic`` travels with the data.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from app.api.deps import get_ehr
from app.config.clinic import CLINIC_LOCATION
from app.ehr.base import EHRProvider, EHRUnavailableError
from app.schemas.domain import Appointment, AppointmentStatus, Patient
from app.utils.scheduling import clinic_date

router = APIRouter(prefix="/api", tags=["dashboard"])


class PatientView(BaseModel):
    model_config = ConfigDict(frozen=True)

    reference: str
    full_name: str
    date_of_birth: date
    phone: str | None
    email: str | None
    postal_code: str | None
    synthetic: bool = True


class MedicationView(BaseModel):
    model_config = ConfigDict(frozen=True)

    medication_request_id: str
    display_name: str
    #: Verbatim from the record, or null when the record has none. Never
    #: assembled, never summarised (SAFETY.md).
    dosage_instruction: str | None
    status: str
    prescriber_name: str | None
    refills_remaining: int | None


class AppointmentView(BaseModel):
    model_config = ConfigDict(frozen=True)

    appointment_id: str
    status: str
    appointment_type: str
    start: datetime
    end: datetime
    duration_minutes: int
    patient_ref: str
    practitioner_ref: str
    practitioner_name: str
    reason: str | None


class ConditionView(BaseModel):
    model_config = ConfigDict(frozen=True)

    condition_id: str
    display_name: str
    clinical_status: str


class AllergyView(BaseModel):
    model_config = ConfigDict(frozen=True)

    allergy_id: str
    substance: str
    reaction: str | None
    criticality: str | None


class PatientDetail(BaseModel):
    model_config = ConfigDict(frozen=True)

    patient: PatientView
    medications: list[MedicationView]
    appointments: list[AppointmentView]
    conditions: list[ConditionView]
    allergies: list[AllergyView]


class PractitionerView(BaseModel):
    model_config = ConfigDict(frozen=True)

    reference: str
    display_name: str
    specialty: str


def _patient_view(patient: Patient) -> PatientView:
    return PatientView(
        reference=patient.reference,
        full_name=patient.full_name,
        date_of_birth=patient.date_of_birth,
        phone=patient.phone,
        email=patient.email,
        postal_code=patient.postal_code,
        synthetic=patient.is_synthetic,
    )


@router.get("/patients", response_model=list[PatientView])
async def search_patients(
    query: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=50, ge=1, le=200),
    ehr: EHRProvider = Depends(get_ehr),
) -> list[PatientView]:
    try:
        patients = await ehr.list_patients(query=query, limit=limit)
    except EHRUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return [_patient_view(p) for p in patients]


@router.get("/patients/{patient_id}", response_model=PatientDetail)
async def patient_detail(patient_id: str, ehr: EHRProvider = Depends(get_ehr)) -> PatientDetail:
    reference = patient_id if patient_id.startswith("Patient/") else f"Patient/{patient_id}"
    try:
        patient = await ehr.get_patient(reference)
        if patient is None:
            raise HTTPException(status_code=404, detail=f"unknown patient {reference!r}")
        medications = await ehr.get_medications(reference)
        appointments = await ehr.get_appointments(
            reference, statuses=(AppointmentStatus.BOOKED, AppointmentStatus.FULFILLED)
        )
        conditions = await ehr.get_conditions(reference)
        allergies = await ehr.get_allergies(reference)
    except EHRUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return PatientDetail(
        patient=_patient_view(patient),
        medications=[
            MedicationView(
                medication_request_id=m.medication_request_id,
                display_name=m.display_name,
                dosage_instruction=m.dosage_instruction,
                status=m.status,
                prescriber_name=m.prescriber_name,
                refills_remaining=m.refills_remaining,
            )
            for m in medications
        ],
        appointments=[_appointment_view(a) for a in appointments],
        conditions=[
            ConditionView(
                condition_id=c.condition_id,
                display_name=c.display_name,
                clinical_status=c.clinical_status,
            )
            for c in conditions
        ],
        allergies=[
            AllergyView(
                allergy_id=a.allergy_id,
                substance=a.substance,
                reaction=a.reaction,
                criticality=a.criticality,
            )
            for a in allergies
        ],
    )


def _appointment_view(appointment: Appointment) -> AppointmentView:
    return AppointmentView(
        appointment_id=appointment.appointment_id,
        status=appointment.status.value,
        appointment_type=appointment.appointment_type.value,
        start=appointment.start,
        end=appointment.end,
        duration_minutes=int((appointment.end - appointment.start).total_seconds() // 60),
        patient_ref=appointment.patient_ref,
        practitioner_ref=appointment.practitioner_ref,
        practitioner_name=appointment.practitioner_name,
        reason=appointment.reason,
    )


@router.get("/appointments", response_model=list[AppointmentView])
async def appointments(
    start_date: date | None = None,
    end_date: date | None = None,
    practitioner_ref: str | None = None,
    include_cancelled: bool = False,
    limit: int = Query(default=200, ge=1, le=500),
    ehr: EHRProvider = Depends(get_ehr),
) -> list[AppointmentView]:
    """The clinic schedule over a date range. Defaults to the coming week."""
    today = clinic_date(datetime.now(UTC))
    start = start_date or today
    end = end_date or (start + timedelta(days=7))
    if end < start:
        raise HTTPException(status_code=422, detail="end_date is before start_date")

    statuses = (
        (AppointmentStatus.BOOKED, AppointmentStatus.CANCELLED, AppointmentStatus.FULFILLED)
        if include_cancelled
        else (AppointmentStatus.BOOKED, AppointmentStatus.FULFILLED)
    )
    try:
        found = await ehr.list_appointments(
            start_date=start,
            end_date=end,
            practitioner_ref=practitioner_ref,
            statuses=statuses,
            limit=limit,
        )
    except EHRUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return [_appointment_view(a) for a in found]


@router.get("/providers", response_model=list[PractitionerView])
async def providers(ehr: EHRProvider = Depends(get_ehr)) -> list[PractitionerView]:
    practitioners = await ehr.get_practitioners()
    return [
        PractitionerView(reference=p.reference, display_name=p.display_name, specialty=p.specialty)
        for p in practitioners
    ]


@router.get("/clinic")
async def clinic() -> dict[str, str]:
    """Who the demo clinic is. Fictional, and the response says so."""
    return {
        "name": CLINIC_LOCATION.name,
        "address": f"{CLINIC_LOCATION.address_line}, {CLINIC_LOCATION.city}, "
        f"{CLINIC_LOCATION.state} {CLINIC_LOCATION.postal_code}",
        "phone": CLINIC_LOCATION.phone,
        "disclaimer": "Fictional clinic. Synthetic data only. Not for clinical use.",
    }
