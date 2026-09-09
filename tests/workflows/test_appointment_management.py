"""Appointment lookup, cancellation, and rescheduling (Phase 5).

Runs against both EHR providers. The acceptance criteria are behavioural:
cancellation must release the slot, rescheduling must release the old one and
take the new one atomically, and every operation must be verified and audited.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from tests.conftest import JOHN_SMITH, JOHN_SMITH_DOB

from app.agents.state import SessionState
from app.ehr.base import EHRProvider
from app.schemas.domain import AppointmentStatus, AppointmentType, AuditAction
from app.services.audit_service import AuditService
from app.services.base import NotVerifiedError
from app.services.escalation_service import EscalationService
from app.services.patient_service import PatientService
from app.services.scheduling_service import SchedulingService
from app.services.verification_service import VerificationService
from app.workflows.appointment_management import (
    AppointmentManagementWorkflow,
    ManagementAction,
    ManagementInput,
    ManagementState,
)
from app.workflows.base import AwaitedInput, WorkflowStatus

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


@pytest.fixture
def escalations() -> EscalationService:
    return EscalationService()


@pytest.fixture
def audit() -> AuditService:
    return AuditService()


@pytest.fixture
def workflow(
    ehr: EHRProvider, escalations: EscalationService, audit: AuditService
) -> AppointmentManagementWorkflow:
    return AppointmentManagementWorkflow(
        verification=VerificationService(PatientService(ehr), escalations),
        scheduling=SchedulingService(ehr),
        escalations=escalations,
        audit=audit,
    )


@pytest.fixture
def session() -> SessionState:
    return SessionState("sess-manage")


async def _identify(
    workflow: AppointmentManagementWorkflow,
    session: SessionState,
    action: ManagementAction,
):
    await workflow.start(session, action=action)
    return await workflow.advance(
        session,
        ManagementInput(full_name="John Smith", date_of_birth=JOHN_SMITH_DOB),
        now=NOW,
    )


class TestLookup:
    async def test_when_is_my_appointment_answers_when_who_and_where(
        self, workflow: AppointmentManagementWorkflow, session: SessionState
    ) -> None:
        result = await _identify(workflow, session, ManagementAction.LOOKUP)

        assert result.status is WorkflowStatus.COMPLETED
        assert result.appointment is not None
        # The seeded demo appointment: Dr. Patel, diabetes follow-up.
        assert "Dr. Sarah Patel" in result.message
        assert "Oakwood Family Medicine" in result.message
        assert "diabetes follow up" in result.message.lower()
        assert session.active_workflow is None

    async def test_a_patient_with_nothing_booked_is_offered_a_booking(
        self, workflow: AppointmentManagementWorkflow, session: SessionState, ehr: EHRProvider
    ) -> None:
        existing = await SchedulingService(ehr).get_upcoming_appointments(JOHN_SMITH, now=NOW)
        await ehr.cancel_appointment(existing[0].appointment_id)

        result = await _identify(workflow, session, ManagementAction.LOOKUP)
        assert result.status is WorkflowStatus.COMPLETED
        assert "don't see any upcoming appointments" in result.message

    async def test_several_appointments_are_disambiguated_before_acting(
        self, workflow: AppointmentManagementWorkflow, session: SessionState, ehr: EHRProvider
    ) -> None:
        """Acting on the wrong appointment is worse than asking which."""
        scheduling = SchedulingService(ehr)
        offers = await scheduling.find_offers(
            AppointmentType.FOLLOW_UP,
            NOW.date() + timedelta(days=1),
            NOW.date() + timedelta(days=14),
            now=NOW,
        )
        await scheduling.book(JOHN_SMITH, offers[-1].slot_id, AppointmentType.FOLLOW_UP)

        listing = await _identify(workflow, session, ManagementAction.LOOKUP)
        assert listing.state == ManagementState.SELECTING_APPOINTMENT.value
        assert "1)" in listing.message and "2)" in listing.message

        chosen = await workflow.advance(session, ManagementInput(appointment_choice=1), now=NOW)
        assert chosen.status is WorkflowStatus.COMPLETED
        assert chosen.appointment is not None


class TestCancellation:
    async def test_cancelling_releases_the_slot(
        self, workflow: AppointmentManagementWorkflow, session: SessionState, ehr: EHRProvider
    ) -> None:
        confirming = await _identify(workflow, session, ManagementAction.CANCEL)
        assert confirming.state == ManagementState.CONFIRMING_CANCEL.value
        assert confirming.awaiting is AwaitedInput.CONFIRMATION
        original = confirming.appointment
        assert original is not None

        result = await workflow.advance(session, ManagementInput(confirm=True), now=NOW)

        assert result.status is WorkflowStatus.COMPLETED
        assert result.appointment is not None
        assert result.appointment.status is AppointmentStatus.CANCELLED
        assert await SchedulingService(ehr).get_upcoming_appointments(JOHN_SMITH, now=NOW) == []

        # Acceptance: the released time is bookable again.
        freed = await ehr.get_available_slots(
            original.appointment_type,
            NOW.date() + timedelta(days=1),
            NOW.date() + timedelta(days=14),
            practitioner_ref=original.practitioner_ref,
            limit=200,
        )
        assert original.start in [slot.start for slot in freed]

    async def test_declining_leaves_the_appointment_alone(
        self, workflow: AppointmentManagementWorkflow, session: SessionState, ehr: EHRProvider
    ) -> None:
        await _identify(workflow, session, ManagementAction.CANCEL)
        result = await workflow.advance(session, ManagementInput(confirm=False), now=NOW)

        assert result.status is WorkflowStatus.COMPLETED
        assert "left it as it is" in result.message
        still_booked = await SchedulingService(ehr).get_upcoming_appointments(JOHN_SMITH, now=NOW)
        assert len(still_booked) == 1

    async def test_cancellation_requires_explicit_confirmation(
        self, workflow: AppointmentManagementWorkflow, session: SessionState, ehr: EHRProvider
    ) -> None:
        await _identify(workflow, session, ManagementAction.CANCEL)
        result = await workflow.advance(session, ManagementInput(), now=NOW)

        assert result.state == ManagementState.CONFIRMING_CANCEL.value
        assert len(await SchedulingService(ehr).get_upcoming_appointments(JOHN_SMITH, now=NOW)) == 1


class TestRescheduling:
    async def test_rescheduling_moves_the_appointment_and_frees_the_old_slot(
        self, workflow: AppointmentManagementWorkflow, session: SessionState, ehr: EHRProvider
    ) -> None:
        offered = await _identify(workflow, session, ManagementAction.RESCHEDULE)
        assert offered.state == ManagementState.OFFERING_SLOTS.value
        assert offered.offers

        original = (await SchedulingService(ehr).get_upcoming_appointments(JOHN_SMITH, now=NOW))[0]
        target = offered.offers[0]

        confirming = await workflow.advance(session, ManagementInput(slot_choice=1), now=NOW)
        assert confirming.state == ManagementState.CONFIRMING_RESCHEDULE.value

        result = await workflow.advance(session, ManagementInput(confirm=True), now=NOW)

        assert result.status is WorkflowStatus.COMPLETED
        assert result.appointment is not None
        assert result.appointment.start == target.start
        assert result.appointment.appointment_type is original.appointment_type

        # Exactly one booking survives, and the old time is free again.
        booked = await SchedulingService(ehr).get_upcoming_appointments(JOHN_SMITH, now=NOW)
        assert [a.appointment_id for a in booked] == [result.appointment.appointment_id]

        freed = await ehr.get_available_slots(
            original.appointment_type,
            NOW.date() + timedelta(days=1),
            NOW.date() + timedelta(days=14),
            practitioner_ref=original.practitioner_ref,
            limit=200,
        )
        assert original.start in [slot.start for slot in freed]

    async def test_a_taken_slot_leaves_the_original_appointment_in_place(
        self, workflow: AppointmentManagementWorkflow, session: SessionState, ehr: EHRProvider
    ) -> None:
        """A failed move must not lose the appointment the patient already had."""
        offered = await _identify(workflow, session, ManagementAction.RESCHEDULE)
        await workflow.advance(session, ManagementInput(slot_choice=1), now=NOW)

        rival = await PatientService(ehr).register_new_patient("Rival", "Booker", date(1990, 1, 1))
        await SchedulingService(ehr).book(
            rival.reference, offered.offers[0].slot_id, AppointmentType.DIABETES_FOLLOW_UP
        )

        result = await workflow.advance(session, ManagementInput(confirm=True), now=NOW)

        assert result.status is WorkflowStatus.AWAITING_INPUT
        assert "still in place" in result.message
        booked = await SchedulingService(ehr).get_upcoming_appointments(JOHN_SMITH, now=NOW)
        assert len(booked) == 1

    async def test_rejecting_every_alternative_escalates_without_changing_anything(
        self, workflow: AppointmentManagementWorkflow, session: SessionState, ehr: EHRProvider
    ) -> None:
        await _identify(workflow, session, ManagementAction.RESCHEDULE)
        result = await workflow.advance(session, ManagementInput(none_suitable=True), now=NOW)

        assert result.status is WorkflowStatus.ESCALATED
        assert result.escalation_id is not None
        assert "unchanged" in result.message
        assert len(await SchedulingService(ehr).get_upcoming_appointments(JOHN_SMITH, now=NOW)) == 1

    async def test_declining_the_confirmation_reoffers(
        self, workflow: AppointmentManagementWorkflow, session: SessionState
    ) -> None:
        await _identify(workflow, session, ManagementAction.RESCHEDULE)
        await workflow.advance(session, ManagementInput(slot_choice=1), now=NOW)
        declined = await workflow.advance(session, ManagementInput(confirm=False), now=NOW)
        assert declined.state == ManagementState.OFFERING_SLOTS.value
        assert declined.offers


class TestVerificationIsRequired:
    async def test_nothing_is_reachable_before_verification(
        self, workflow: AppointmentManagementWorkflow, session: SessionState
    ) -> None:
        result = await workflow.advance(
            session, ManagementInput(action=ManagementAction.CANCEL), now=NOW
        )
        assert result.state == ManagementState.COLLECTING_IDENTITY.value
        assert result.appointment is None
        assert session.is_verified is False

    async def test_an_unverified_session_cannot_reach_the_scheduling_service(
        self, session: SessionState, ehr: EHRProvider
    ) -> None:
        from app.services.access_control import require_verified_patient

        with pytest.raises(NotVerifiedError):
            await SchedulingService(ehr).cancel(require_verified_patient(session), "appt-anything")

    async def test_repeated_failure_escalates(
        self, workflow: AppointmentManagementWorkflow, session: SessionState
    ) -> None:
        await workflow.start(session, action=ManagementAction.CANCEL)
        for _ in range(3):
            result = await workflow.advance(
                session,
                ManagementInput(full_name="Jane Doe", date_of_birth=date(1970, 1, 1)),
                now=NOW,
            )
        assert result.status is WorkflowStatus.ESCALATED


class TestAuditTrail:
    async def test_a_lookup_records_who_read_what(
        self, workflow: AppointmentManagementWorkflow, session: SessionState, audit: AuditService
    ) -> None:
        await _identify(workflow, session, ManagementAction.LOOKUP)

        actions = audit.store.actions()
        assert AuditAction.VERIFICATION_ATTEMPTED in actions
        assert AuditAction.VERIFICATION_SUCCEEDED in actions
        assert AuditAction.APPOINTMENTS_READ in actions

        read = next(e for e in audit.store.all() if e.action is AuditAction.APPOINTMENTS_READ)
        assert read.patient_ref == JOHN_SMITH
        assert read.session_id == session.session_id

    async def test_a_cancellation_is_audited_with_the_appointment_id(
        self, workflow: AppointmentManagementWorkflow, session: SessionState, audit: AuditService
    ) -> None:
        await _identify(workflow, session, ManagementAction.CANCEL)
        result = await workflow.advance(session, ManagementInput(confirm=True), now=NOW)
        assert result.appointment is not None

        event = next(e for e in audit.store.all() if e.action is AuditAction.APPOINTMENT_CANCELLED)
        assert event.resource_id == result.appointment.appointment_id
        assert event.patient_ref == JOHN_SMITH
        assert event.outcome == "success"

    async def test_a_reschedule_records_what_it_replaced(
        self, workflow: AppointmentManagementWorkflow, session: SessionState, audit: AuditService
    ) -> None:
        await _identify(workflow, session, ManagementAction.RESCHEDULE)
        await workflow.advance(session, ManagementInput(slot_choice=1), now=NOW)
        result = await workflow.advance(session, ManagementInput(confirm=True), now=NOW)
        assert result.appointment is not None

        event = next(
            e for e in audit.store.all() if e.action is AuditAction.APPOINTMENT_RESCHEDULED
        )
        assert event.resource_id == result.appointment.appointment_id
        assert event.detail is not None and "replaced" in event.detail

    async def test_failed_verification_is_audited_as_denied(
        self, workflow: AppointmentManagementWorkflow, session: SessionState, audit: AuditService
    ) -> None:
        await workflow.start(session, action=ManagementAction.LOOKUP)
        await workflow.advance(
            session,
            ManagementInput(full_name="Jane Doe", date_of_birth=date(1970, 1, 1)),
            now=NOW,
        )
        failed = next(e for e in audit.store.all() if e.action is AuditAction.VERIFICATION_FAILED)
        assert failed.outcome == "denied"
        assert failed.patient_ref is None

    async def test_the_audit_trail_holds_no_clinical_content(
        self, workflow: AppointmentManagementWorkflow, session: SessionState, audit: AuditService
    ) -> None:
        """It records that a record was touched, not what was in it."""
        await _identify(workflow, session, ManagementAction.LOOKUP)
        serialised = " ".join(e.model_dump_json() for e in audit.store.all())
        for leaked in ("Smith", "John", "1985-02-15", "Metformin", "diabetes"):
            assert leaked not in serialised
