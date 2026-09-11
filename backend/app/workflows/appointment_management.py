"""Managing an existing appointment (docs/call-flows.md).

Answers "when is my appointment?", "who am I seeing?", "where is it?", and
handles cancellation and rescheduling.

Two things distinguish this from booking. First, the patient may have more than
one appointment, so the workflow disambiguates before acting -- cancelling the
wrong one is worse than asking. Second, every operation is audited: a
cancellation that nobody can account for afterwards is a problem in any
healthcare system.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.agents.state import SessionState
from app.config.clinic import CLINIC_LOCATION
from app.observability.logging import get_logger
from app.schemas.domain import Appointment, AuditAction, EscalationCategory
from app.services.access_control import require_verified_patient
from app.services.audit_service import AuditService
from app.services.base import ConflictError, NotFoundError, NotOwnedError, UpstreamUnavailableError
from app.services.escalation_service import EscalationService
from app.services.scheduling_service import DEFAULT_OFFER_COUNT, SchedulingService
from app.services.verification_service import (
    SecondFactorType,
    VerificationService,
)
from app.utils.formatting import format_appointment, format_day, format_time
from app.workflows.base import (
    AwaitedInput,
    SlotOffer,
    WorkflowMemory,
    WorkflowResponse,
    WorkflowStatus,
    begin_request,
)
from app.workflows.identity import IdentityCollector, IdentityOutcome

logger = get_logger(__name__)

WORKFLOW_NAME = "appointment_management"

RESCHEDULE_SEARCH_DAYS = 30


class ManagementAction(StrEnum):
    LOOKUP = "lookup"
    CANCEL = "cancel"
    RESCHEDULE = "reschedule"


class ManagementState(StrEnum):
    COLLECTING_IDENTITY = "COLLECTING_IDENTITY"
    AWAITING_SECOND_FACTOR = "AWAITING_SECOND_FACTOR"
    CHOOSING_ACTION = "CHOOSING_ACTION"
    SELECTING_APPOINTMENT = "SELECTING_APPOINTMENT"
    OFFERING_SLOTS = "OFFERING_SLOTS"
    CONFIRMING_CANCEL = "CONFIRMING_CANCEL"
    CONFIRMING_RESCHEDULE = "CONFIRMING_RESCHEDULE"
    ANSWERED = "ANSWERED"
    CANCELLED = "CANCELLED"
    RESCHEDULED = "RESCHEDULED"
    ESCALATED = "ESCALATED"


#: States that mean the last thing the caller asked for is done with, so the
#: next thing they ask for is a new request rather than a continuation.
FINISHED_STATES = frozenset(
    {
        ManagementState.ANSWERED.value,
        ManagementState.CANCELLED.value,
        ManagementState.RESCHEDULED.value,
    }
)


class ManagementInput(BaseModel):
    """What the orchestrator extracted from this turn."""

    model_config = ConfigDict(frozen=True)

    utterance: str = ""
    full_name: str | None = None
    date_of_birth: date | None = None
    second_factor_type: SecondFactorType | None = None
    second_factor_value: str | None = None
    action: ManagementAction | None = None
    #: 1-based ordinal when the patient has several appointments.
    appointment_choice: int | None = None
    slot_choice: int | None = None
    none_suitable: bool = False
    confirm: bool | None = None


class AppointmentManagementWorkflow:
    """Look up, cancel, or move an existing appointment."""

    name = WORKFLOW_NAME

    def __init__(
        self,
        verification: VerificationService,
        scheduling: SchedulingService,
        escalations: EscalationService,
        audit: AuditService | None = None,
    ) -> None:
        self.verification = verification
        self.scheduling = scheduling
        self.escalations = escalations
        self.audit = audit or AuditService()
        self.identity = IdentityCollector(verification, self.audit)

    # ---------------------------------------------------------------- entry
    async def start(
        self, session: SessionState, action: ManagementAction | None = None
    ) -> WorkflowResponse:
        memory = self._memory(session)
        session.active_workflow = self.name
        self._begin_request(session, memory)
        if action is not None:
            memory.set("action", action.value)
        return self._prompt(session)

    async def advance(
        self, session: SessionState, turn: ManagementInput, now: datetime | None = None
    ) -> WorkflowResponse:
        memory = self._memory(session)
        session.active_workflow = self.name
        # Before the state is read: the request this memory belongs to may
        # already be over, and what follows must act on the new one.
        self._begin_request(session, memory)
        state = ManagementState(memory.get("state", ManagementState.COLLECTING_IDENTITY.value))
        moment = now or datetime.now(UTC)
        if turn.action is not None:
            memory.set("action", turn.action.value)

        try:
            match state:
                case ManagementState.COLLECTING_IDENTITY:
                    return await self._handle_identity(session, turn, moment)
                case ManagementState.AWAITING_SECOND_FACTOR:
                    return await self._handle_second_factor(session, turn, moment)
                case ManagementState.CHOOSING_ACTION | ManagementState.SELECTING_APPOINTMENT:
                    return await self._handle_selection(session, turn, moment)
                case ManagementState.CONFIRMING_CANCEL:
                    return await self._handle_cancel_confirmation(session, turn, moment)
                case ManagementState.OFFERING_SLOTS:
                    return await self._handle_slot_choice(session, turn, moment)
                case ManagementState.CONFIRMING_RESCHEDULE:
                    return await self._handle_reschedule_confirmation(session, turn, moment)
                case _:
                    return self._prompt(session)
        except UpstreamUnavailableError as exc:
            return self._escalate(
                session,
                turn,
                EscalationCategory.SYSTEM_UNCERTAINTY,
                f"Scheduling system unavailable: {exc}",
                "I'm having trouble reaching our scheduling system. Let me pass you to "
                "our front desk.",
            )

    # ----------------------------------------------------------- identity
    async def _handle_identity(
        self, session: SessionState, turn: ManagementInput, now: datetime
    ) -> WorkflowResponse:
        result = await self.identity.submit_identity(session, turn.full_name, turn.date_of_birth)
        if result.outcome is IdentityOutcome.VERIFIED:
            return await self._load_appointments(session, turn, now)
        if result.outcome is IdentityOutcome.LOCKED_OUT:
            return self._locked_out(session, result.escalation_id)
        return self._awaiting(
            session,
            ManagementState.AWAITING_SECOND_FACTOR
            if result.outcome is IdentityOutcome.NEEDS_SECOND_FACTOR
            else ManagementState.COLLECTING_IDENTITY,
            result.message,
            result.awaiting or AwaitedInput.IDENTITY,
        )

    async def _handle_second_factor(
        self, session: SessionState, turn: ManagementInput, now: datetime
    ) -> WorkflowResponse:
        result = await self.identity.submit_second_factor(
            session, turn.second_factor_type, turn.second_factor_value
        )
        if result.outcome is IdentityOutcome.VERIFIED:
            return await self._load_appointments(session, turn, now)
        if result.outcome is IdentityOutcome.LOCKED_OUT:
            return self._locked_out(session, result.escalation_id)
        return self._awaiting(
            session,
            ManagementState.AWAITING_SECOND_FACTOR,
            result.message,
            result.awaiting or AwaitedInput.SECOND_FACTOR,
        )

    # -------------------------------------------------------- appointments
    async def _load_appointments(
        self, session: SessionState, turn: ManagementInput, now: datetime
    ) -> WorkflowResponse:
        patient_ref = require_verified_patient(session)
        appointments = await self.scheduling.get_upcoming_appointments(patient_ref, now=now)
        self.audit.record(
            AuditAction.APPOINTMENTS_READ,
            session_id=session.session_id,
            patient_ref=patient_ref,
            resource_type="Appointment",
            detail=f"{len(appointments)} upcoming",
        )

        memory = self._memory(session)
        memory.set("appointments", [a.model_dump(mode="json") for a in appointments])

        if not appointments:
            memory.set("state", ManagementState.ANSWERED.value)
            session.active_workflow = None
            return self._respond(
                session,
                ManagementState.ANSWERED,
                WorkflowStatus.COMPLETED,
                "I don't see any upcoming appointments for you. Would you like to book one?",
            )

        if len(appointments) > 1 and turn.appointment_choice is None:
            listing = "; ".join(
                f"{position}) {format_appointment(a.start, a.practitioner_name)}"
                for position, a in enumerate(appointments, start=1)
            )
            return self._respond(
                session,
                ManagementState.SELECTING_APPOINTMENT,
                WorkflowStatus.AWAITING_INPUT,
                f"You have a few coming up: {listing}. Which one did you mean?",
            )

        chosen = self._resolve_choice(appointments, turn.appointment_choice)
        if chosen is None:
            listing = "; ".join(
                f"{position}) {format_appointment(a.start, a.practitioner_name)}"
                for position, a in enumerate(appointments, start=1)
            )
            return self._respond(
                session,
                ManagementState.SELECTING_APPOINTMENT,
                WorkflowStatus.AWAITING_INPUT,
                f"Sorry, which one did you mean? {listing}.",
            )

        memory.set("selected", chosen.model_dump(mode="json"))
        return await self._act(session, turn, chosen, now)

    async def _handle_selection(
        self, session: SessionState, turn: ManagementInput, now: datetime
    ) -> WorkflowResponse:
        memory = self._memory(session)
        appointments = self._stored_appointments(memory)
        if not appointments:
            return await self._load_appointments(session, turn, now)

        chosen = self._resolve_choice(appointments, turn.appointment_choice)
        if chosen is None:
            listing = "; ".join(
                f"{position}) {format_appointment(a.start, a.practitioner_name)}"
                for position, a in enumerate(appointments, start=1)
            )
            return self._respond(
                session,
                ManagementState.SELECTING_APPOINTMENT,
                WorkflowStatus.AWAITING_INPUT,
                f"Sorry, which one did you mean? {listing}.",
            )

        memory.set("selected", chosen.model_dump(mode="json"))
        return await self._act(session, turn, chosen, now)

    async def _act(
        self,
        session: SessionState,
        turn: ManagementInput,
        appointment: Appointment,
        now: datetime,
    ) -> WorkflowResponse:
        memory = self._memory(session)
        action = ManagementAction(memory.get("action", ManagementAction.LOOKUP.value))

        if action is ManagementAction.LOOKUP:
            memory.set("state", ManagementState.ANSWERED.value)
            session.active_workflow = None
            return self._respond(
                session,
                ManagementState.ANSWERED,
                WorkflowStatus.COMPLETED,
                self._describe(appointment),
                appointment=appointment,
            )

        if action is ManagementAction.CANCEL:
            return self._respond(
                session,
                ManagementState.CONFIRMING_CANCEL,
                WorkflowStatus.AWAITING_INPUT,
                f"That's {format_appointment(appointment.start, appointment.practitioner_name)}. "
                "Shall I cancel it?",
                awaiting=AwaitedInput.CONFIRMATION,
                appointment=appointment,
            )

        return await self._offer_alternatives(session, turn, appointment, now)

    # ------------------------------------------------------------- cancel
    async def _handle_cancel_confirmation(
        self, session: SessionState, turn: ManagementInput, now: datetime
    ) -> WorkflowResponse:
        memory = self._memory(session)
        appointment = Appointment.model_validate(memory.get("selected"))

        if turn.confirm is None:
            return self._respond(
                session,
                ManagementState.CONFIRMING_CANCEL,
                WorkflowStatus.AWAITING_INPUT,
                "Would you like me to cancel it?",
                awaiting=AwaitedInput.CONFIRMATION,
                appointment=appointment,
            )

        if turn.confirm is False:
            memory.set("state", ManagementState.ANSWERED.value)
            session.active_workflow = None
            return self._respond(
                session,
                ManagementState.ANSWERED,
                WorkflowStatus.COMPLETED,
                "No problem, I've left it as it is. Anything else?",
                appointment=appointment,
            )

        patient_ref = require_verified_patient(session)
        try:
            cancelled = await self.scheduling.cancel(patient_ref, appointment.appointment_id)
        except (NotOwnedError, NotFoundError):
            return self._appointment_gone(session, turn)

        self.audit.record(
            AuditAction.APPOINTMENT_CANCELLED,
            session_id=session.session_id,
            patient_ref=patient_ref,
            resource_type="Appointment",
            resource_id=cancelled.appointment_id,
        )
        memory.set("state", ManagementState.CANCELLED.value)
        session.active_workflow = None
        return self._respond(
            session,
            ManagementState.CANCELLED,
            WorkflowStatus.COMPLETED,
            f"That's cancelled — {format_day(appointment.start)} at "
            f"{format_time(appointment.start)}. Would you like to rebook now?",
            appointment=cancelled,
        )

    # --------------------------------------------------------- reschedule
    async def _offer_alternatives(
        self,
        session: SessionState,
        turn: ManagementInput,
        appointment: Appointment,
        now: datetime,
    ) -> WorkflowResponse:
        start = now.date() + timedelta(days=1)
        slots = await self.scheduling.find_offers(
            appointment_type=appointment.appointment_type,
            start_date=start,
            end_date=start + timedelta(days=RESCHEDULE_SEARCH_DAYS),
            practitioner_ref=appointment.practitioner_ref,
            count=DEFAULT_OFFER_COUNT,
            now=now,
        )
        if not slots:
            # Same clinician has nothing; try the whole clinic before giving up.
            slots = await self.scheduling.find_offers(
                appointment_type=appointment.appointment_type,
                start_date=start,
                end_date=start + timedelta(days=RESCHEDULE_SEARCH_DAYS),
                count=DEFAULT_OFFER_COUNT,
                now=now,
            )

        if not slots:
            return self._escalate(
                session,
                turn,
                EscalationCategory.ADMINISTRATIVE,
                "No alternative availability for a reschedule.",
                "I can't find another suitable time at the moment. Let me pass you to "
                "our front desk, who can look at other options.",
            )

        offers = tuple(
            SlotOffer(
                index=position,
                slot_id=slot.slot_id,
                practitioner_ref=slot.practitioner_ref,
                practitioner_name=slot.practitioner_name,
                start=slot.start,
                duration_minutes=slot.duration_minutes,
            )
            for position, slot in enumerate(slots, start=1)
        )
        memory = self._memory(session)
        memory.set("offers", [o.model_dump(mode="json") for o in offers])
        listing = "; ".join(f"{o.index}) {o.label}" for o in offers)
        return self._respond(
            session,
            ManagementState.OFFERING_SLOTS,
            WorkflowStatus.AWAITING_INPUT,
            f"I can move that to: {listing}. Which would you prefer?",
            awaiting=AwaitedInput.SLOT_CHOICE,
            offers=offers,
        )

    async def _handle_slot_choice(
        self, session: SessionState, turn: ManagementInput, now: datetime
    ) -> WorkflowResponse:
        memory = self._memory(session)
        offers = self._stored_offers(memory)
        appointment = Appointment.model_validate(memory.get("selected"))

        if turn.none_suitable:
            return self._escalate(
                session,
                turn,
                EscalationCategory.ADMINISTRATIVE,
                "Patient rejected all alternative times offered for a reschedule.",
                "Let me pass you to our front desk so they can find something that suits "
                "you better. Your existing appointment is unchanged.",
            )

        chosen = next((o for o in offers if o.index == turn.slot_choice), None)
        if chosen is None:
            listing = "; ".join(f"{o.index}) {o.label}" for o in offers)
            return self._respond(
                session,
                ManagementState.OFFERING_SLOTS,
                WorkflowStatus.AWAITING_INPUT,
                f"Sorry, which time would you like? {listing}.",
                awaiting=AwaitedInput.SLOT_CHOICE,
                offers=offers,
            )

        memory.set("chosen", chosen.model_dump(mode="json"))
        return self._respond(
            session,
            ManagementState.CONFIRMING_RESCHEDULE,
            WorkflowStatus.AWAITING_INPUT,
            f"So I'll move your appointment from "
            f"{format_day(appointment.start)} to {chosen.label}. Shall I go ahead?",
            awaiting=AwaitedInput.CONFIRMATION,
            offers=(chosen,),
        )

    async def _handle_reschedule_confirmation(
        self, session: SessionState, turn: ManagementInput, now: datetime
    ) -> WorkflowResponse:
        memory = self._memory(session)
        appointment = Appointment.model_validate(memory.get("selected"))
        chosen = SlotOffer.model_validate(memory.get("chosen"))

        if turn.confirm is None:
            return self._respond(
                session,
                ManagementState.CONFIRMING_RESCHEDULE,
                WorkflowStatus.AWAITING_INPUT,
                f"Shall I move it to {chosen.label}?",
                awaiting=AwaitedInput.CONFIRMATION,
                offers=(chosen,),
            )

        if turn.confirm is False:
            return await self._offer_alternatives(session, turn, appointment, now)

        patient_ref = require_verified_patient(session)
        try:
            moved = await self.scheduling.reschedule(
                patient_ref, appointment.appointment_id, chosen.slot_id
            )
        except ConflictError:
            # The original booking is untouched; offer what is still free.
            retry = await self._offer_alternatives(session, turn, appointment, now)
            return retry.model_copy(
                update={
                    "message": (
                        "I'm sorry — that time went while we were talking, and your "
                        f"original appointment is still in place. {retry.message}"
                    )
                }
            )
        except (NotOwnedError, NotFoundError):
            return self._appointment_gone(session, turn)

        self.audit.record(
            AuditAction.APPOINTMENT_RESCHEDULED,
            session_id=session.session_id,
            patient_ref=patient_ref,
            resource_type="Appointment",
            resource_id=moved.appointment_id,
            detail=f"replaced {appointment.appointment_id}",
        )
        memory.set("state", ManagementState.RESCHEDULED.value)
        session.active_workflow = None
        return self._respond(
            session,
            ManagementState.RESCHEDULED,
            WorkflowStatus.COMPLETED,
            f"Done — you're now booked for {chosen.label}. Your previous time has been released.",
            appointment=moved,
        )

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _describe(appointment: Appointment) -> str:
        """Answers "when", "who with", and "where" in one sentence."""
        return (
            f"Your next appointment is {format_day(appointment.start)} at "
            f"{format_time(appointment.start)} with {appointment.practitioner_name}, "
            f"at {CLINIC_LOCATION.name}, {CLINIC_LOCATION.address_line}, "
            f"{CLINIC_LOCATION.city}. It's a "
            f"{appointment.appointment_type.display.lower()}."
        )

    @staticmethod
    def _resolve_choice(appointments: list[Appointment], choice: int | None) -> Appointment | None:
        if len(appointments) == 1 and choice is None:
            return appointments[0]
        if choice is None:
            return None
        if 1 <= choice <= len(appointments):
            return appointments[choice - 1]
        return None

    @staticmethod
    def _stored_appointments(memory: WorkflowMemory) -> list[Appointment]:
        return [Appointment.model_validate(raw) for raw in memory.get("appointments", [])]

    @staticmethod
    def _stored_offers(memory: WorkflowMemory) -> tuple[SlotOffer, ...]:
        return tuple(SlotOffer.model_validate(raw) for raw in memory.get("offers", []))

    def _memory(self, session: SessionState) -> WorkflowMemory:
        return WorkflowMemory(session.workflow_state, self.name)

    def _appointment_gone(self, session: SessionState, turn: ManagementInput) -> WorkflowResponse:
        """The appointment vanished mid-conversation, or was never theirs.

        The patient is not told which: confirming that an appointment exists
        for someone else is a disclosure (SAFETY.md).
        """
        self.audit.record(
            AuditAction.ACCESS_DENIED,
            session_id=session.session_id,
            patient_ref=session.patient_ref,
            resource_type="Appointment",
            outcome="denied",
        )
        self._memory(session).set("state", ManagementState.ANSWERED.value)
        session.active_workflow = None
        return self._respond(
            session,
            ManagementState.ANSWERED,
            WorkflowStatus.COMPLETED,
            "I can't find that appointment on your record any more. Let me know if "
            "you'd like me to check what's currently booked.",
        )

    def _awaiting(
        self,
        session: SessionState,
        state: ManagementState,
        message: str,
        awaiting: AwaitedInput,
    ) -> WorkflowResponse:
        return self._respond(
            session, state, WorkflowStatus.AWAITING_INPUT, message, awaiting=awaiting
        )

    def _locked_out(self, session: SessionState, escalation_id: str | None) -> WorkflowResponse:
        session.active_workflow = None
        return self._respond(
            session,
            ManagementState.ESCALATED,
            WorkflowStatus.ESCALATED,
            "I haven't been able to confirm your identity, so I'm passing you to our front desk.",
            escalation_id=escalation_id,
        )

    def _escalate(
        self,
        session: SessionState,
        turn: ManagementInput,
        category: EscalationCategory,
        summary: str,
        message: str,
    ) -> WorkflowResponse:
        escalation = self.escalations.create(
            category=category,
            summary=summary,
            session_id=session.session_id,
            patient_ref=session.patient_ref,
            verification_state=session.verification,
            patient_question=turn.utterance or None,
            ai_action="No appointment change made",
        )
        self.audit.record(
            AuditAction.ESCALATION_CREATED,
            session_id=session.session_id,
            patient_ref=session.patient_ref,
            resource_type="Escalation",
            resource_id=escalation.escalation_id,
        )
        session.active_workflow = None
        return self._respond(
            session,
            ManagementState.ESCALATED,
            WorkflowStatus.ESCALATED,
            message,
            escalation_id=escalation.escalation_id,
        )

    @staticmethod
    def _begin_request(session: SessionState, memory: WorkflowMemory) -> None:
        begin_request(
            memory,
            finished=FINISHED_STATES,
            fresh=(
                ManagementState.CHOOSING_ACTION.value
                if session.is_verified
                else ManagementState.COLLECTING_IDENTITY.value
            ),
        )

    def _prompt(self, session: SessionState) -> WorkflowResponse:
        memory = self._memory(session)
        state = ManagementState(memory.get("state", ManagementState.COLLECTING_IDENTITY.value))
        if state is ManagementState.COLLECTING_IDENTITY:
            return self._awaiting(
                session,
                state,
                "Happy to help. Could I take your full name and date of birth?",
                AwaitedInput.IDENTITY,
            )
        if state is ManagementState.AWAITING_SECOND_FACTOR:
            return self._awaiting(
                session,
                state,
                "Could I take the last four digits of the phone number on file?",
                AwaitedInput.SECOND_FACTOR,
            )
        return self._respond(
            session,
            state,
            WorkflowStatus.AWAITING_INPUT,
            "What would you like to do with your appointment?",
        )

    def _respond(
        self,
        session: SessionState,
        state: ManagementState,
        status: WorkflowStatus,
        message: str,
        awaiting: AwaitedInput | None = None,
        offers: tuple[SlotOffer, ...] = (),
        appointment: Appointment | None = None,
        escalation_id: str | None = None,
    ) -> WorkflowResponse:
        self._memory(session).set("state", state.value)
        return WorkflowResponse(
            workflow=self.name,
            state=state.value,
            status=status,
            message=message,
            awaiting=awaiting,
            offers=offers,
            appointment=appointment,
            escalation_id=escalation_id,
        )
