"""The existing-patient booking workflow (Phase 4, docs/call-flows.md).

Runs against both EHR providers. No model is involved: the workflow takes
typed input, so every branch is testable deterministically and for free.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta

import pytest
from tests.conftest import JOHN_SMITH, JOHN_SMITH_DOB

from app.agents.state import SessionState
from app.ehr.base import EHRProvider
from app.safety.models import SafetyCategory
from app.schemas.domain import AppointmentType, EscalationCategory
from app.services.escalation_service import EscalationService
from app.services.patient_service import PatientService
from app.services.safety_service import SafetyService
from app.services.scheduling_service import SchedulingService
from app.services.verification_service import SecondFactorType, VerificationService
from app.utils.formatting import local
from app.workflows.base import AwaitedInput, WorkflowStatus
from app.workflows.existing_patient_booking import (
    BookingInput,
    BookingState,
    ExistingPatientBookingWorkflow,
)

#: Before the seeded slot window opens, so offers are always available.
NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


@pytest.fixture
def escalations() -> EscalationService:
    return EscalationService()


@pytest.fixture
def workflow(ehr: EHRProvider, escalations: EscalationService) -> ExistingPatientBookingWorkflow:
    return ExistingPatientBookingWorkflow(
        verification=VerificationService(PatientService(ehr), escalations),
        scheduling=SchedulingService(ehr),
        escalations=escalations,
    )


@pytest.fixture
def session() -> SessionState:
    return SessionState("sess-booking")


async def _verify(
    workflow: ExistingPatientBookingWorkflow, session: SessionState, **extra: object
) -> object:
    return await workflow.advance(
        session,
        BookingInput(full_name="John Smith", date_of_birth=JOHN_SMITH_DOB, **extra),  # type: ignore[arg-type]
        now=NOW,
    )


class TestHappyPath:
    async def test_a_full_booking_conversation(
        self,
        workflow: ExistingPatientBookingWorkflow,
        session: SessionState,
        ehr: EHRProvider,
    ) -> None:
        """DEMO.md scenario 1, turn by turn."""
        opening = await workflow.start(session)
        assert opening.state == BookingState.COLLECTING_IDENTITY.value
        assert opening.awaiting is AwaitedInput.IDENTITY

        identified = await _verify(workflow, session)
        assert session.is_verified is True
        assert identified.awaiting is AwaitedInput.REASON

        offered = await workflow.advance(
            session,
            BookingInput(reason="diabetes follow-up", practitioner_name="Dr. Patel"),
            now=NOW,
        )
        assert offered.state == BookingState.OFFERING_SLOTS.value
        assert offered.awaiting is AwaitedInput.SLOT_CHOICE
        assert 1 <= len(offered.offers) <= 3
        assert all(o.practitioner_name == "Dr. Sarah Patel" for o in offered.offers)
        assert all(o.duration_minutes == 30 for o in offered.offers)

        chosen_start = offered.offers[0].start
        confirming = await workflow.advance(session, BookingInput(slot_choice=1), now=NOW)
        assert confirming.state == BookingState.CONFIRMING.value
        assert confirming.awaiting is AwaitedInput.CONFIRMATION

        booked = await workflow.advance(session, BookingInput(confirm=True), now=NOW)
        assert booked.status is WorkflowStatus.COMPLETED
        assert booked.appointment is not None
        assert booked.appointment.appointment_type is AppointmentType.DIABETES_FOLLOW_UP
        assert booked.appointment.start == chosen_start
        assert session.active_workflow is None

        # Acceptance: the booking mutated EHR state and survives a re-read.
        persisted = await ehr.get_appointments(JOHN_SMITH)
        assert booked.appointment.appointment_id in [a.appointment_id for a in persisted]

    async def test_the_appointment_type_fixes_the_duration(
        self, workflow: ExistingPatientBookingWorkflow, session: SessionState
    ) -> None:
        await _verify(workflow, session)
        offered = await workflow.advance(session, BookingInput(reason="annual physical"), now=NOW)
        assert all(o.duration_minutes == 45 for o in offered.offers)

        await workflow.advance(session, BookingInput(slot_choice=1), now=NOW)
        booked = await workflow.advance(session, BookingInput(confirm=True), now=NOW)
        assert booked.appointment is not None
        assert (booked.appointment.end - booked.appointment.start) == timedelta(minutes=45)

    async def test_an_unrecognised_reason_falls_back_without_failing(
        self, workflow: ExistingPatientBookingWorkflow, session: SessionState
    ) -> None:
        await _verify(workflow, session)
        offered = await workflow.advance(
            session, BookingInput(reason="something I can't describe"), now=NOW
        )
        assert offered.offers
        assert all(o.duration_minutes == 20 for o in offered.offers)  # FOLLOW_UP

    async def test_details_given_early_are_used(
        self, workflow: ExistingPatientBookingWorkflow, session: SessionState
    ) -> None:
        """ "I'm John Smith, born 15 Feb 1985, I need a diabetes check" in one breath."""
        result = await workflow.advance(
            session,
            BookingInput(
                full_name="John Smith",
                date_of_birth=JOHN_SMITH_DOB,
                reason="diabetes follow-up",
            ),
            now=NOW,
        )
        assert result.state == BookingState.OFFERING_SLOTS.value
        assert result.offers


class TestVerificationBranches:
    async def test_booking_cannot_start_without_identity(
        self, workflow: ExistingPatientBookingWorkflow, session: SessionState
    ) -> None:
        result = await workflow.advance(session, BookingInput(reason="diabetes follow-up"), now=NOW)
        assert result.state == BookingState.COLLECTING_IDENTITY.value
        assert result.offers == ()
        assert session.is_verified is False

    async def test_wrong_details_reprompt_without_revealing_anything(
        self, workflow: ExistingPatientBookingWorkflow, session: SessionState
    ) -> None:
        unknown = await workflow.advance(
            session,
            BookingInput(full_name="Jane Doe", date_of_birth=date(1970, 1, 1)),
            now=NOW,
        )
        wrong_dob = await workflow.advance(
            session,
            BookingInput(full_name="John Smith", date_of_birth=date(1985, 2, 16)),
            now=NOW,
        )
        assert unknown.message == wrong_dob.message
        assert unknown.state == wrong_dob.state == BookingState.COLLECTING_IDENTITY.value

    async def test_three_failures_escalate_to_the_front_desk(
        self,
        workflow: ExistingPatientBookingWorkflow,
        session: SessionState,
        escalations: EscalationService,
    ) -> None:
        for _ in range(3):
            result = await workflow.advance(
                session,
                BookingInput(full_name="Jane Doe", date_of_birth=date(1970, 1, 1)),
                now=NOW,
            )
        assert result.status is WorkflowStatus.ESCALATED
        assert result.escalation_id is not None
        created = escalations.store.for_session(session.session_id)
        assert created[0].category is EscalationCategory.FAILED_VERIFICATION

    async def test_ambiguous_details_route_through_a_second_factor(
        self, workflow: ExistingPatientBookingWorkflow, session: SessionState
    ) -> None:
        first = await workflow.advance(
            session,
            BookingInput(full_name="Robert Johnson", date_of_birth=date(1990, 6, 21)),
            now=NOW,
        )
        assert first.state == BookingState.AWAITING_SECOND_FACTOR.value
        assert first.awaiting is AwaitedInput.SECOND_FACTOR

        wrong = await workflow.advance(
            session,
            BookingInput(
                second_factor_type=SecondFactorType.PHONE_LAST_FOUR,
                second_factor_value="9999",
            ),
            now=NOW,
        )
        assert wrong.state == BookingState.AWAITING_SECOND_FACTOR.value

        right = await workflow.advance(
            session,
            BookingInput(
                second_factor_type=SecondFactorType.PHONE_LAST_FOUR,
                second_factor_value="0411",
                reason="follow up",
            ),
            now=NOW,
        )
        assert session.is_verified is True
        assert right.state == BookingState.OFFERING_SLOTS.value


class TestSlotSelection:
    async def test_offers_are_few_and_distinct(
        self, workflow: ExistingPatientBookingWorkflow, session: SessionState
    ) -> None:
        await _verify(workflow, session)
        offered = await workflow.advance(session, BookingInput(reason="follow up"), now=NOW)
        assert 1 <= len(offered.offers) <= 3
        assert len({o.slot_id for o in offered.offers}) == len(offered.offers)
        assert [o.index for o in offered.offers] == list(range(1, len(offered.offers) + 1))

    async def test_an_unparsed_choice_reprompts_with_the_same_offers(
        self, workflow: ExistingPatientBookingWorkflow, session: SessionState
    ) -> None:
        await _verify(workflow, session)
        offered = await workflow.advance(session, BookingInput(reason="follow up"), now=NOW)

        confused = await workflow.advance(session, BookingInput(slot_choice=99), now=NOW)
        assert confused.state == BookingState.OFFERING_SLOTS.value
        assert [o.slot_id for o in confused.offers] == [o.slot_id for o in offered.offers]

    async def test_rejecting_every_offer_offers_different_times(
        self, workflow: ExistingPatientBookingWorkflow, session: SessionState
    ) -> None:
        """ "None of those work" has to produce times they have not refused.

        Widening the end of the search window does nothing on its own: the
        earliest times still come back first, so a caller heard the same three
        slots read out again in a different sentence. Observed live, and the
        old version of this test asserted only that the window had changed --
        which it had, uselessly.
        """
        await _verify(workflow, session)
        first = await workflow.advance(session, BookingInput(reason="follow up"), now=NOW)
        again = await workflow.advance(session, BookingInput(none_suitable=True), now=NOW)

        assert again.state == BookingState.OFFERING_SLOTS.value
        assert again.offers
        rejected = {offer.slot_id for offer in first.offers}
        assert not rejected & {offer.slot_id for offer in again.offers}, (
            "offered a time the caller had just turned down"
        )
        assert min(o.start for o in again.offers) > max(o.start for o in first.offers)

    async def test_the_second_set_spans_several_days(
        self, workflow: ExistingPatientBookingWorkflow, session: SessionState
    ) -> None:
        """Three clinicians on one morning is one morning, not three choices.

        Fine as an opening offer -- some callers do want the earliest thing
        going. Once they have said no to that morning, what varies has to be
        the day.
        """
        await _verify(workflow, session)
        await workflow.advance(session, BookingInput(reason="follow up"), now=NOW)

        again = await workflow.advance(session, BookingInput(none_suitable=True), now=NOW)

        days = {local(offer.start).date() for offer in again.offers}
        assert len(days) == len(again.offers), f"all on the same day: {again.message}"

    async def test_rejecting_twice_keeps_moving_forward(
        self, workflow: ExistingPatientBookingWorkflow, session: SessionState
    ) -> None:
        await _verify(workflow, session)
        await workflow.advance(session, BookingInput(reason="follow up"), now=NOW)
        second = await workflow.advance(session, BookingInput(none_suitable=True), now=NOW)

        third = await workflow.advance(session, BookingInput(none_suitable=True), now=NOW)

        assert third.offers
        assert min(o.start for o in third.offers) > max(o.start for o in second.offers)

    async def test_repeated_rejection_hands_over_to_a_human(
        self, workflow: ExistingPatientBookingWorkflow, session: SessionState
    ) -> None:
        await _verify(workflow, session)
        await workflow.advance(session, BookingInput(reason="follow up"), now=NOW)
        for _ in range(3):
            result = await workflow.advance(session, BookingInput(none_suitable=True), now=NOW)
        assert result.status is WorkflowStatus.ESCALATED

    async def test_declining_the_confirmation_reoffers(
        self, workflow: ExistingPatientBookingWorkflow, session: SessionState
    ) -> None:
        await _verify(workflow, session)
        await workflow.advance(session, BookingInput(reason="follow up"), now=NOW)
        await workflow.advance(session, BookingInput(slot_choice=1), now=NOW)

        declined = await workflow.advance(session, BookingInput(confirm=False), now=NOW)
        assert declined.state == BookingState.OFFERING_SLOTS.value
        assert declined.offers


class TestProviderPreference:
    async def test_a_named_clinician_is_honoured(
        self, workflow: ExistingPatientBookingWorkflow, session: SessionState
    ) -> None:
        await _verify(workflow, session)
        offered = await workflow.advance(
            session,
            BookingInput(reason="follow up", practitioner_name="Dr. Chen"),
            now=NOW,
        )
        assert {o.practitioner_name for o in offered.offers} == {"Dr. Emily Chen"}

    async def test_an_unknown_clinician_does_not_derail_the_booking(
        self, workflow: ExistingPatientBookingWorkflow, session: SessionState
    ) -> None:
        await _verify(workflow, session)
        offered = await workflow.advance(
            session,
            BookingInput(reason="follow up", practitioner_name="Dr. Nobody"),
            now=NOW,
        )
        assert offered.offers
        assert "couldn't find that clinician" in offered.message


class TestContention:
    async def test_a_slot_taken_mid_conversation_is_handled_gracefully(
        self, workflow: ExistingPatientBookingWorkflow, session: SessionState, ehr: EHRProvider
    ) -> None:
        await _verify(workflow, session)
        offered = await workflow.advance(session, BookingInput(reason="follow up"), now=NOW)
        await workflow.advance(session, BookingInput(slot_choice=1), now=NOW)

        # Someone else books it while the patient is deciding.
        rival = await PatientService(ehr).register_new_patient("Rival", "Booker", date(1990, 1, 1))
        await SchedulingService(ehr).book(
            rival.reference, offered.offers[0].slot_id, AppointmentType.FOLLOW_UP
        )

        result = await workflow.advance(session, BookingInput(confirm=True), now=NOW)
        assert result.status is WorkflowStatus.AWAITING_INPUT
        assert "was taken" in result.message
        assert result.offers
        assert offered.offers[0].slot_id not in [o.slot_id for o in result.offers]

    async def test_two_sessions_confirming_the_same_slot_yield_one_booking(
        self, ehr: EHRProvider, escalations: EscalationService
    ) -> None:
        """Acceptance: concurrent booking of one slot -- exactly one succeeds."""
        scheduling = SchedulingService(ehr)
        patients = PatientService(ehr)
        verification = VerificationService(patients, escalations)

        async def make_workflow() -> ExistingPatientBookingWorkflow:
            return ExistingPatientBookingWorkflow(verification, scheduling, escalations)

        first_session = SessionState("sess-a")
        second_session = SessionState("sess-b")
        rival = await patients.register_new_patient("Casey", "Lin", date(1988, 9, 9))

        workflow_a = await make_workflow()
        workflow_b = await make_workflow()

        await workflow_a.advance(
            first_session,
            BookingInput(full_name="John Smith", date_of_birth=JOHN_SMITH_DOB),
            now=NOW,
        )
        offers_a = await workflow_a.advance(
            first_session, BookingInput(reason="follow up"), now=NOW
        )
        await workflow_b.advance(
            second_session,
            BookingInput(full_name=rival.full_name, date_of_birth=date(1988, 9, 9)),
            now=NOW,
        )
        offers_b = await workflow_b.advance(
            second_session, BookingInput(reason="follow up"), now=NOW
        )
        assert offers_a.offers[0].slot_id == offers_b.offers[0].slot_id

        await workflow_a.advance(first_session, BookingInput(slot_choice=1), now=NOW)
        await workflow_b.advance(second_session, BookingInput(slot_choice=1), now=NOW)

        results = await asyncio.gather(
            workflow_a.advance(first_session, BookingInput(confirm=True), now=NOW),
            workflow_b.advance(second_session, BookingInput(confirm=True), now=NOW),
        )
        completed = [r for r in results if r.status is WorkflowStatus.COMPLETED]
        reoffered = [r for r in results if r.status is WorkflowStatus.AWAITING_INPUT]
        assert len(completed) == 1
        assert len(reoffered) == 1


class TestStateHandling:
    async def test_a_half_finished_booking_is_serialisable(
        self, workflow: ExistingPatientBookingWorkflow, session: SessionState
    ) -> None:
        """Workflow memory must survive a round trip to storage (Phase 11)."""
        await _verify(workflow, session)
        await workflow.advance(session, BookingInput(reason="follow up"), now=NOW)

        restored = json.loads(json.dumps(session.workflow_state))
        assert restored == session.workflow_state
        assert restored[f"{workflow.name}.state"] == BookingState.OFFERING_SLOTS.value

    async def test_resuming_repeats_the_outstanding_question(
        self, workflow: ExistingPatientBookingWorkflow, session: SessionState
    ) -> None:
        await _verify(workflow, session)
        offered = await workflow.advance(session, BookingInput(reason="follow up"), now=NOW)

        resumed = await workflow.start(session)
        assert resumed.state == BookingState.OFFERING_SLOTS.value
        assert [o.slot_id for o in resumed.offers] == [o.slot_id for o in offered.offers]

    async def test_booking_uses_the_session_patient_not_a_supplied_one(
        self, workflow: ExistingPatientBookingWorkflow, session: SessionState
    ) -> None:
        """The workflow never takes a patient reference from its input at all."""
        assert "patient_ref" not in BookingInput.model_fields
        await _verify(workflow, session)
        await workflow.advance(session, BookingInput(reason="follow up"), now=NOW)
        await workflow.advance(session, BookingInput(slot_choice=1), now=NOW)
        booked = await workflow.advance(session, BookingInput(confirm=True), now=NOW)
        assert booked.appointment is not None
        assert booked.appointment.patient_ref == session.patient_ref == JOHN_SMITH


class TestAuditTrail:
    async def test_a_completed_booking_is_audited(
        self, ehr: EHRProvider, escalations: EscalationService, session: SessionState
    ) -> None:
        from app.schemas.domain import AuditAction
        from app.services.audit_service import AuditService

        audit = AuditService()
        workflow = ExistingPatientBookingWorkflow(
            verification=VerificationService(PatientService(ehr), escalations),
            scheduling=SchedulingService(ehr),
            escalations=escalations,
            audit=audit,
        )
        await _verify(workflow, session)
        await workflow.advance(session, BookingInput(reason="follow up"), now=NOW)
        await workflow.advance(session, BookingInput(slot_choice=1), now=NOW)
        booked = await workflow.advance(session, BookingInput(confirm=True), now=NOW)
        assert booked.appointment is not None

        event = next(e for e in audit.store.all() if e.action is AuditAction.APPOINTMENT_BOOKED)
        assert event.resource_id == booked.appointment.appointment_id
        assert event.patient_ref == JOHN_SMITH
        assert AuditAction.VERIFICATION_SUCCEEDED in audit.store.actions()


class TestSafetyComposition:
    """Safety runs before the workflow. Phase 9 makes this the orchestrator's job."""

    async def test_a_clinical_question_never_reaches_the_workflow(
        self,
        workflow: ExistingPatientBookingWorkflow,
        session: SessionState,
        escalations: EscalationService,
    ) -> None:
        safety = SafetyService(escalations=escalations)
        await _verify(workflow, session)
        before = await workflow.advance(session, BookingInput(reason="follow up"), now=NOW)

        utterance = "Actually, my Lisinopril makes me dizzy — should I take half?"
        evaluation = safety.evaluate(utterance, session, medication_display="Lisinopril 10 mg")

        assert evaluation.is_refusal is True
        assert evaluation.decision.category is SafetyCategory.DOSE_MODIFICATION

        # The workflow was never advanced, so the booking is untouched and
        # resumable once the clinical matter is handed over.
        resumed = await workflow.start(session)
        assert resumed.state == before.state
        assert [o.slot_id for o in resumed.offers] == [o.slot_id for o in before.offers]

    async def test_an_ordinary_booking_utterance_is_allowed_through(
        self, session: SessionState, escalations: EscalationService
    ) -> None:
        safety = SafetyService(escalations=escalations)
        evaluation = safety.evaluate(
            "I'd like to book a diabetes follow-up with Dr. Patel next week", session
        )
        assert evaluation.is_refusal is False
