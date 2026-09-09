"""Identity handling is identical across every workflow.

The wording carries a guarantee: an unknown name and a wrong date of birth must
be indistinguishable (ADR 003). If one workflow drifts — a friendlier retry
here, an extra hint there — that guarantee holds in three places and leaks in
the fourth. One implementation, asserted from the outside.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.agents.state import SessionState
from app.ehr.base import EHRProvider
from app.services.audit_service import AuditService
from app.services.escalation_service import EscalationService
from app.services.medication_service import MedicationService
from app.services.patient_service import PatientService
from app.services.refill_service import RefillService
from app.services.scheduling_service import SchedulingService
from app.services.verification_service import VerificationService
from app.workflows.appointment_management import (
    AppointmentManagementWorkflow,
    ManagementAction,
    ManagementInput,
)
from app.workflows.existing_patient_booking import BookingInput, ExistingPatientBookingWorkflow
from app.workflows.medication_lookup import MedicationLookupInput, MedicationLookupWorkflow
from app.workflows.refill_request import RefillRequestInput, RefillRequestWorkflow

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
UNKNOWN = {"full_name": "Jane Doe", "date_of_birth": date(1970, 1, 1)}
WRONG_DOB = {"full_name": "John Smith", "date_of_birth": date(1985, 2, 16)}


async def _messages_for(ehr: EHRProvider, details: dict[str, object]) -> list[str]:
    """Submit the same wrong details to every workflow and collect the replies."""
    messages: list[str] = []

    escalations = EscalationService()
    audit = AuditService()
    verification = VerificationService(PatientService(ehr), escalations)
    scheduling = SchedulingService(ehr)
    medications = MedicationService(ehr)

    booking = ExistingPatientBookingWorkflow(verification, scheduling, escalations, audit)
    session = SessionState("sess-booking")
    await booking.start(session)
    messages.append(
        (await booking.advance(session, BookingInput(**details), now=NOW)).message  # type: ignore[arg-type]
    )

    management = AppointmentManagementWorkflow(
        VerificationService(PatientService(ehr), escalations), scheduling, escalations, audit
    )
    session = SessionState("sess-manage")
    await management.start(session, action=ManagementAction.LOOKUP)
    messages.append(
        (
            await management.advance(session, ManagementInput(**details), now=NOW)  # type: ignore[arg-type]
        ).message
    )

    lookup = MedicationLookupWorkflow(
        VerificationService(PatientService(ehr), escalations), medications, escalations, audit
    )
    session = SessionState("sess-lookup")
    await lookup.start(session)
    messages.append(
        (
            await lookup.advance(session, MedicationLookupInput(**details), now=NOW)  # type: ignore[arg-type]
        ).message
    )

    refill = RefillRequestWorkflow(
        VerificationService(PatientService(ehr), escalations),
        medications,
        RefillService(),
        escalations,
        audit,
    )
    session = SessionState("sess-refill")
    messages.append(
        (
            await refill.advance(session, RefillRequestInput(**details), now=NOW)  # type: ignore[arg-type]
        ).message
    )

    return messages


async def test_every_workflow_reprompts_identically(ehr: EHRProvider) -> None:
    messages = await _messages_for(ehr, UNKNOWN)
    assert len(set(messages)) == 1, f"identity wording has diverged: {set(messages)}"


async def test_an_unknown_name_and_a_wrong_dob_are_indistinguishable_everywhere(
    ehr: EHRProvider,
) -> None:
    unknown = await _messages_for(ehr, UNKNOWN)
    wrong_dob = await _messages_for(ehr, WRONG_DOB)
    assert unknown == wrong_dob


@pytest.mark.parametrize("details", [UNKNOWN, WRONG_DOB])
async def test_no_reprompt_hints_at_what_was_wrong(
    ehr: EHRProvider, details: dict[str, object]
) -> None:
    for message in await _messages_for(ehr, details):
        lowered = message.lower()
        for leak in ("date of birth doesn't", "no such patient", "not registered", "we have"):
            assert leak not in lowered
