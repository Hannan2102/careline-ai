"""Domain models.

These are what services, tools, workflows, and the agent see. FHIR resource
shapes stop at the EHR adapter boundary (ADR 001) -- nothing above this line
knows whether the data came from HAPI, Epic, or the in-memory provider.
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AppointmentType(StrEnum):
    """Visit types offered by the clinic. Duration is fixed per type."""

    NEW_PATIENT = "NEW_PATIENT"
    FOLLOW_UP = "FOLLOW_UP"
    SICK_VISIT = "SICK_VISIT"
    ANNUAL_PHYSICAL = "ANNUAL_PHYSICAL"
    DIABETES_FOLLOW_UP = "DIABETES_FOLLOW_UP"
    HYPERTENSION_FOLLOW_UP = "HYPERTENSION_FOLLOW_UP"
    MEDICATION_FOLLOW_UP = "MEDICATION_FOLLOW_UP"

    @property
    def duration_minutes(self) -> int:
        return APPOINTMENT_DURATIONS[self]

    @property
    def display(self) -> str:
        return self.value.replace("_", " ").title()


APPOINTMENT_DURATIONS: dict[AppointmentType, int] = {
    AppointmentType.NEW_PATIENT: 45,
    AppointmentType.FOLLOW_UP: 20,
    AppointmentType.SICK_VISIT: 30,
    AppointmentType.ANNUAL_PHYSICAL: 45,
    AppointmentType.DIABETES_FOLLOW_UP: 30,
    AppointmentType.HYPERTENSION_FOLLOW_UP: 20,
    AppointmentType.MEDICATION_FOLLOW_UP: 20,
}


class AppointmentStatus(StrEnum):
    BOOKED = "booked"
    CANCELLED = "cancelled"
    FULFILLED = "fulfilled"
    NOSHOW = "noshow"


class SlotStatus(StrEnum):
    FREE = "free"
    BUSY = "busy"


class VerificationState(StrEnum):
    """Session-scoped identity state. Only the verification service may set it."""

    UNVERIFIED = "UNVERIFIED"
    PENDING_SECOND_FACTOR = "PENDING_SECOND_FACTOR"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"


class Priority(StrEnum):
    URGENT = "urgent"
    CLINICAL = "clinical"
    PATIENT_REQUESTED = "patient-requested"
    SYSTEM_UNCERTAINTY = "system-uncertainty"
    ADMINISTRATIVE = "administrative"


class EscalationCategory(StrEnum):
    """Why a conversation was handed to a human (SAFETY.md)."""

    CLINICAL = "clinical"
    FAILED_VERIFICATION = "failed-verification"
    PATIENT_REQUESTED = "patient-requested"
    SYSTEM_UNCERTAINTY = "system-uncertainty"
    ADMINISTRATIVE = "administrative"


class RefillStatus(StrEnum):
    PENDING_REVIEW = "PENDING_REVIEW"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
    CANCELLED = "CANCELLED"


# --------------------------------------------------------------------------
# Clinical / scheduling entities
# --------------------------------------------------------------------------


class Patient(BaseModel):
    """A synthetic patient. Never contains real patient information."""

    model_config = ConfigDict(frozen=True)

    reference: str = Field(description="FHIR reference, e.g. 'Patient/demo-john-smith'")
    given_name: str
    family_name: str
    date_of_birth: date
    phone: str | None = None
    email: str | None = None
    postal_code: str | None = None
    is_synthetic: bool = True

    @property
    def full_name(self) -> str:
        return f"{self.given_name} {self.family_name}"

    @property
    def phone_last_four(self) -> str | None:
        digits = "".join(c for c in (self.phone or "") if c.isdigit())
        return digits[-4:] if len(digits) >= 4 else None


class Practitioner(BaseModel):
    model_config = ConfigDict(frozen=True)

    reference: str
    given_name: str
    family_name: str
    specialty: str
    prefix: str = "Dr."

    @property
    def display_name(self) -> str:
        return f"{self.prefix} {self.given_name} {self.family_name}"


class ClinicLocation(BaseModel):
    model_config = ConfigDict(frozen=True)

    reference: str
    name: str
    address_line: str
    city: str
    state: str
    postal_code: str
    phone: str


class AvailableSlot(BaseModel):
    """A bookable start time, already checked against the requested duration."""

    model_config = ConfigDict(frozen=True)

    slot_id: str
    practitioner_ref: str
    practitioner_name: str
    start: datetime
    end: datetime

    @property
    def duration_minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


class Appointment(BaseModel):
    model_config = ConfigDict(frozen=True)

    appointment_id: str
    status: AppointmentStatus
    appointment_type: AppointmentType
    start: datetime
    end: datetime
    patient_ref: str
    practitioner_ref: str
    practitioner_name: str
    location_ref: str
    reason: str | None = None
    slot_ids: tuple[str, ...] = ()


class MedicationSummary(BaseModel):
    """An active prescription as recorded in the EHR.

    ``dosage_instruction`` is copied **verbatim** from the source record and is
    ``None`` when the record has no instruction text. It is never assembled from
    structured components -- constructing a dosage is a clinical act (SAFETY.md).
    """

    model_config = ConfigDict(frozen=True)

    medication_request_id: str
    patient_ref: str
    display_name: str
    dosage_instruction: str | None
    status: str
    prescriber_ref: str | None = None
    prescriber_name: str | None = None
    refills_remaining: int | None = None

    @property
    def has_dosage_on_file(self) -> bool:
        return bool(self.dosage_instruction and self.dosage_instruction.strip())


class Condition(BaseModel):
    model_config = ConfigDict(frozen=True)

    condition_id: str
    patient_ref: str
    display_name: str
    clinical_status: str = "active"


class Coverage(BaseModel):
    """A patient's insurance cover, as the clinic has it on file.

    Every field is what the *clinic* recorded, not what the insurer would say
    today. Eligibility, remaining deductible and the price of a particular
    visit are all live questions for the payer, and none of them are answerable
    from this record -- which is why the workflow that reads it quotes the plan
    and hands anything about money to the front desk (SAFETY.md).
    """

    model_config = ConfigDict(frozen=True)

    coverage_id: str
    patient_ref: str
    #: The plan as printed on the card, e.g. "Blue Shield PPO (demo)".
    plan_name: str
    #: The insurer. Often the same organisation as the plan, and not always.
    payer_name: str | None = None
    #: The member number on the card. PHI, and disclosed only to a verified
    #: caller like any other record field.
    subscriber_id: str | None = None
    #: FHIR calls this `status`; "active" or "cancelled" are the two that
    #: matter here.
    status: str = "active"
    #: When cover ends, when the record says so.
    period_end: date | None = None

    @property
    def is_active(self) -> bool:
        return self.status == "active"


class Allergy(BaseModel):
    model_config = ConfigDict(frozen=True)

    allergy_id: str
    patient_ref: str
    substance: str
    reaction: str | None = None
    criticality: str | None = None


# --------------------------------------------------------------------------
# Application-side entities (not FHIR resources -- see FHIR.md)
# --------------------------------------------------------------------------


class RefillRequest(BaseModel):
    """A request awaiting clinician review. Never an authorisation."""

    model_config = ConfigDict(frozen=True)

    refill_request_id: str
    patient_ref: str
    medication_request_id: str
    medication_display: str
    status: RefillStatus = RefillStatus.PENDING_REVIEW
    requested_at: datetime
    session_id: str | None = None


class Escalation(BaseModel):
    """A structured handoff to a human. Preserves context so staff need not restart."""

    model_config = ConfigDict(frozen=True)

    escalation_id: str
    category: EscalationCategory
    priority: Priority
    destination: str
    summary: str
    patient_ref: str | None = None
    verification_state: VerificationState = VerificationState.UNVERIFIED
    medication_display: str | None = None
    patient_question: str | None = None
    ai_action: str = "No clinical advice provided"
    created_at: datetime
    session_id: str | None = None


class AuditAction(StrEnum):
    """What was attempted. Reads are audited as well as writes."""

    VERIFICATION_ATTEMPTED = "verification.attempted"
    VERIFICATION_SUCCEEDED = "verification.succeeded"
    VERIFICATION_FAILED = "verification.failed"
    APPOINTMENTS_READ = "appointment.read"
    APPOINTMENT_BOOKED = "appointment.booked"
    APPOINTMENT_CANCELLED = "appointment.cancelled"
    APPOINTMENT_RESCHEDULED = "appointment.rescheduled"
    MEDICATIONS_READ = "medication.read"
    COVERAGE_READ = "coverage.read"
    REFILL_REQUESTED = "refill.requested"
    ESCALATION_CREATED = "escalation.created"
    ACCESS_DENIED = "access.denied"


class AuditEvent(BaseModel):
    """One entry in the append-only audit trail.

    Records that something happened and to which record -- never the clinical
    content itself. ``detail`` is for operational context, not for copying
    demographics or instructions into a second place.
    """

    model_config = ConfigDict(frozen=True)

    event_id: str
    action: AuditAction
    session_id: str | None = None
    patient_ref: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    outcome: Literal["success", "denied", "failure"] = "success"
    detail: str | None = None
    created_at: datetime


class ProviderUsageRecord(BaseModel):
    """One metered unit of provider consumption, priced at record time."""

    model_config = ConfigDict(frozen=True)

    provider: str
    metric: str
    quantity: Decimal
    estimated_cost: Decimal
    session_id: str | None = None
    recorded_at: datetime


# --------------------------------------------------------------------------
# Clinic configuration
# --------------------------------------------------------------------------


class WorkingHours(BaseModel):
    model_config = ConfigDict(frozen=True)

    opens: time
    closes: time
    break_start: time | None = None
    break_end: time | None = None
