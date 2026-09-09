"""Existing-patient appointment booking (docs/call-flows.md).

    identity -> verify -> reason -> classify type -> search -> offer 2-3 times
    -> patient chooses -> confirm -> book

Deterministic throughout. The workflow takes typed input and returns typed
output; extracting "next Tuesday with Dr. Patel" from speech is the
orchestrator's job in Phase 9. That split is what lets every branch here be
tested without a model, and therefore for free.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.agents.state import SessionState
from app.config.clinic import find_practitioner_by_name
from app.observability.logging import get_logger
from app.schemas.domain import AppointmentType, AuditAction, EscalationCategory
from app.services.audit_service import AuditService
from app.services.base import ConflictError, NotFoundError, UpstreamUnavailableError
from app.services.escalation_service import EscalationService
from app.services.scheduling_service import (
    DEFAULT_OFFER_COUNT,
    SchedulingService,
    classify_reason,
)
from app.services.verification_service import (
    SecondFactorType,
    VerificationService,
)
from app.workflows.base import (
    AwaitedInput,
    SlotOffer,
    WorkflowMemory,
    WorkflowResponse,
    WorkflowStatus,
)
from app.workflows.identity import IdentityCollector, IdentityOutcome

logger = get_logger(__name__)

WORKFLOW_NAME = "existing_patient_booking"

#: Initial search window, and how far it widens when nothing is available.
INITIAL_SEARCH_DAYS = 14
WIDENED_SEARCH_DAYS = 45


class BookingState(StrEnum):
    COLLECTING_IDENTITY = "COLLECTING_IDENTITY"
    AWAITING_SECOND_FACTOR = "AWAITING_SECOND_FACTOR"
    COLLECTING_REASON = "COLLECTING_REASON"
    OFFERING_SLOTS = "OFFERING_SLOTS"
    CONFIRMING = "CONFIRMING"
    BOOKED = "BOOKED"
    ESCALATED = "ESCALATED"


class BookingInput(BaseModel):
    """What the orchestrator extracted from this turn.

    Every field is optional: the workflow reads only what its current state
    needs, and ignores the rest. A caller volunteering their date of birth
    early is not an error, it is just not useful yet.
    """

    model_config = ConfigDict(frozen=True)

    #: The patient's own words, kept for escalation context.
    utterance: str = ""
    full_name: str | None = None
    date_of_birth: date | None = None
    second_factor_type: SecondFactorType | None = None
    second_factor_value: str | None = None
    reason: str | None = None
    practitioner_name: str | None = None
    #: 1-based ordinal of the offered time the patient chose.
    slot_choice: int | None = None
    #: The patient rejected every offer and wants different times.
    none_suitable: bool = False
    confirm: bool | None = None


class ExistingPatientBookingWorkflow:
    """Books an appointment for a patient who is already registered."""

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
    async def start(self, session: SessionState) -> WorkflowResponse:
        """Begin (or resume) a booking."""
        memory = self._memory(session)
        session.active_workflow = self.name
        if memory.get("state") is None:
            memory.set(
                "state",
                BookingState.COLLECTING_REASON.value
                if session.is_verified
                else BookingState.COLLECTING_IDENTITY.value,
            )
        return self._prompt_for_current_state(session)

    async def advance(
        self, session: SessionState, turn: BookingInput, now: datetime | None = None
    ) -> WorkflowResponse:
        """Move the booking forward by one turn."""
        memory = self._memory(session)
        session.active_workflow = self.name
        state = BookingState(memory.get("state", BookingState.COLLECTING_IDENTITY.value))
        moment = now or datetime.now(UTC)

        # Callers volunteer things before they are asked -- "I'd like a diabetes
        # follow-up with Dr. Patel" arrives while we are still taking their name.
        # Absorb it now, or the workflow asks again a turn later and sounds like
        # it was not listening.
        self._absorb(memory, turn)

        try:
            match state:
                case BookingState.COLLECTING_IDENTITY:
                    return await self._handle_identity(session, turn, moment)
                case BookingState.AWAITING_SECOND_FACTOR:
                    return await self._handle_second_factor(session, turn, moment)
                case BookingState.COLLECTING_REASON:
                    return await self._handle_reason(session, turn, moment)
                case BookingState.OFFERING_SLOTS:
                    return await self._handle_choice(session, turn, moment)
                case BookingState.CONFIRMING:
                    return await self._handle_confirmation(session, turn, moment)
                case BookingState.BOOKED | BookingState.ESCALATED:
                    return self._prompt_for_current_state(session)
        except UpstreamUnavailableError as exc:
            # Degrade to a human, never to a guess.
            return self._escalate(
                session,
                turn,
                EscalationCategory.SYSTEM_UNCERTAINTY,
                f"Scheduling system unavailable: {exc}",
                "I'm having trouble reaching our scheduling system. Let me pass you to "
                "our front desk so they can book this for you.",
            )

    # ----------------------------------------------------------- transitions
    async def _handle_identity(
        self, session: SessionState, turn: BookingInput, now: datetime
    ) -> WorkflowResponse:
        result = await self.identity.submit_identity(session, turn.full_name, turn.date_of_birth)
        if result.outcome is IdentityOutcome.VERIFIED:
            return await self._after_verification(session, turn, now)
        if result.outcome is IdentityOutcome.LOCKED_OUT:
            return self._locked_out(session, result.escalation_id)
        return self._awaiting(
            session,
            BookingState.AWAITING_SECOND_FACTOR
            if result.outcome is IdentityOutcome.NEEDS_SECOND_FACTOR
            else BookingState.COLLECTING_IDENTITY,
            result.message,
            result.awaiting or AwaitedInput.IDENTITY,
        )

    async def _handle_second_factor(
        self, session: SessionState, turn: BookingInput, now: datetime
    ) -> WorkflowResponse:
        result = await self.identity.submit_second_factor(
            session, turn.second_factor_type, turn.second_factor_value
        )
        if result.outcome is IdentityOutcome.VERIFIED:
            return await self._after_verification(session, turn, now)
        if result.outcome is IdentityOutcome.LOCKED_OUT:
            return self._locked_out(session, result.escalation_id)
        return self._awaiting(
            session,
            BookingState.AWAITING_SECOND_FACTOR,
            result.message,
            result.awaiting or AwaitedInput.SECOND_FACTOR,
        )

    async def _after_verification(
        self, session: SessionState, turn: BookingInput, now: datetime
    ) -> WorkflowResponse:
        """Verified. Use a reason already given, or ask for one."""
        if self._memory(session).get("appointment_type") is not None:
            return await self._search_and_offer(session, turn, now)
        return self._awaiting(
            session,
            BookingState.COLLECTING_REASON,
            "Thank you, I've found you. What would you like to be seen about?",
            AwaitedInput.REASON,
        )

    async def _handle_reason(
        self, session: SessionState, turn: BookingInput, now: datetime
    ) -> WorkflowResponse:
        if self._memory(session).get("appointment_type") is None:
            return self._awaiting(
                session,
                BookingState.COLLECTING_REASON,
                "What would you like to be seen about?",
                AwaitedInput.REASON,
            )
        return await self._search_and_offer(session, turn, now)

    @staticmethod
    def _absorb(memory: WorkflowMemory, turn: BookingInput) -> None:
        """Record details supplied ahead of the question that asks for them."""
        if turn.reason and memory.get("appointment_type") is None:
            classification = classify_reason(turn.reason)
            memory.set("reason", turn.reason)
            memory.set("appointment_type", classification.appointment_type.value)
            memory.set("type_was_inferred", classification.is_fallback)

        if turn.practitioner_name:
            practitioner = find_practitioner_by_name(turn.practitioner_name)
            memory.set("practitioner_ref", practitioner.reference if practitioner else None)
            memory.set("practitioner_unmatched", practitioner is None)

    async def _search_and_offer(
        self, session: SessionState, turn: BookingInput, now: datetime
    ) -> WorkflowResponse:
        memory = self._memory(session)
        appointment_type = AppointmentType(memory.get("appointment_type"))
        practitioner_ref = memory.get("practitioner_ref")
        search_days = int(memory.get("search_days", INITIAL_SEARCH_DAYS))

        offers = await self._find_offers(appointment_type, practitioner_ref, search_days, now)

        # Nothing in the initial window: widen once before giving up.
        if not offers and search_days < WIDENED_SEARCH_DAYS:
            search_days = WIDENED_SEARCH_DAYS
            memory.set("search_days", search_days)
            offers = await self._find_offers(appointment_type, practitioner_ref, search_days, now)

        # Still nothing with the requested clinician: try any of them.
        dropped_preference = False
        if not offers and practitioner_ref:
            offers = await self._find_offers(appointment_type, None, search_days, now)
            if offers:
                dropped_preference = True
                memory.set("practitioner_ref", None)

        if not offers:
            return self._escalate(
                session,
                turn,
                EscalationCategory.ADMINISTRATIVE,
                f"No {appointment_type.display} availability within {search_days} days.",
                "I can't find anything suitable in the diary at the moment. Let me pass "
                "you to our front desk, who can look at other options.",
            )

        memory.set("offers", [offer.model_dump(mode="json") for offer in offers])
        memory.set("state", BookingState.OFFERING_SLOTS.value)

        preamble = self._offer_preamble(memory, appointment_type, dropped_preference)
        listing = "; ".join(f"{o.index}) {o.label}" for o in offers)
        return self._respond(
            session,
            BookingState.OFFERING_SLOTS,
            WorkflowStatus.AWAITING_INPUT,
            f"{preamble} {listing}. Which of those works best?",
            awaiting=AwaitedInput.SLOT_CHOICE,
            offers=offers,
        )

    async def _handle_choice(
        self, session: SessionState, turn: BookingInput, now: datetime
    ) -> WorkflowResponse:
        memory = self._memory(session)
        offers = self._stored_offers(memory)

        if turn.none_suitable:
            memory.set("search_days", WIDENED_SEARCH_DAYS)
            memory.set("rejected_count", int(memory.get("rejected_count", 0)) + 1)
            if int(memory.get("rejected_count", 0)) > 2:
                return self._escalate(
                    session,
                    turn,
                    EscalationCategory.ADMINISTRATIVE,
                    "Patient rejected all offered times repeatedly.",
                    "Let me pass you to our front desk so they can find a time that "
                    "suits you better.",
                )
            return await self._search_and_offer(session, turn, now)

        if turn.slot_choice is None:
            listing = "; ".join(f"{o.index}) {o.label}" for o in offers)
            return self._respond(
                session,
                BookingState.OFFERING_SLOTS,
                WorkflowStatus.AWAITING_INPUT,
                f"Just to confirm, which time would you like? {listing}.",
                awaiting=AwaitedInput.SLOT_CHOICE,
                offers=offers,
            )

        chosen = next((o for o in offers if o.index == turn.slot_choice), None)
        if chosen is None:
            listing = "; ".join(f"{o.index}) {o.label}" for o in offers)
            return self._respond(
                session,
                BookingState.OFFERING_SLOTS,
                WorkflowStatus.AWAITING_INPUT,
                f"Sorry, I didn't catch which one you meant. {listing}.",
                awaiting=AwaitedInput.SLOT_CHOICE,
                offers=offers,
            )

        memory.set("chosen", chosen.model_dump(mode="json"))
        memory.set("state", BookingState.CONFIRMING.value)
        return self._respond(
            session,
            BookingState.CONFIRMING,
            WorkflowStatus.AWAITING_INPUT,
            f"That's {chosen.label}. Shall I book that for you?",
            awaiting=AwaitedInput.CONFIRMATION,
            offers=(chosen,),
        )

    async def _handle_confirmation(
        self, session: SessionState, turn: BookingInput, now: datetime
    ) -> WorkflowResponse:
        memory = self._memory(session)
        chosen = SlotOffer.model_validate(memory.get("chosen"))

        if turn.confirm is False:
            memory.set("state", BookingState.OFFERING_SLOTS.value)
            return await self._search_and_offer(session, turn, now)

        if turn.confirm is None:
            return self._respond(
                session,
                BookingState.CONFIRMING,
                WorkflowStatus.AWAITING_INPUT,
                f"Would you like me to book {chosen.label}?",
                awaiting=AwaitedInput.CONFIRMATION,
                offers=(chosen,),
            )

        # The gate: booking uses the session's verified patient, never a
        # reference supplied by the caller or a model.
        from app.services.access_control import require_verified_patient

        patient_ref = require_verified_patient(session)
        appointment_type = AppointmentType(memory.get("appointment_type"))

        try:
            appointment = await self.scheduling.book(
                patient_ref=patient_ref,
                slot_id=chosen.slot_id,
                appointment_type=appointment_type,
                reason=memory.get("reason"),
            )
        except ConflictError:
            # Someone took it, or the patient is already booked then. Re-offer.
            memory.set("state", BookingState.OFFERING_SLOTS.value)
            retry = await self._search_and_offer(session, turn, now)
            return retry.model_copy(
                update={
                    "message": (
                        f"I'm sorry — that time was taken while we were talking. {retry.message}"
                    )
                }
            )
        except NotFoundError:
            memory.set("state", BookingState.OFFERING_SLOTS.value)
            return await self._search_and_offer(session, turn, now)

        self.audit.record(
            AuditAction.APPOINTMENT_BOOKED,
            session_id=session.session_id,
            patient_ref=patient_ref,
            resource_type="Appointment",
            resource_id=appointment.appointment_id,
            detail=appointment_type.value,
        )
        memory.set("state", BookingState.BOOKED.value)
        memory.set("appointment_id", appointment.appointment_id)
        session.active_workflow = None
        logger.info(
            "booking_workflow_completed",
            session_id=session.session_id,
            appointment_id=appointment.appointment_id,
        )
        return self._respond(
            session,
            BookingState.BOOKED,
            WorkflowStatus.COMPLETED,
            f"You're booked in for {chosen.label}. "
            "Please arrive about 15 minutes early. Is there anything else?",
            appointment=appointment,
        )

    # ------------------------------------------------------------- helpers
    async def _find_offers(
        self,
        appointment_type: AppointmentType,
        practitioner_ref: str | None,
        search_days: int,
        now: datetime,
    ) -> tuple[SlotOffer, ...]:
        start = now.date() + timedelta(days=1)
        slots = await self.scheduling.find_offers(
            appointment_type=appointment_type,
            start_date=start,
            end_date=start + timedelta(days=search_days),
            practitioner_ref=practitioner_ref,
            count=DEFAULT_OFFER_COUNT,
            now=now,
        )
        return tuple(
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

    @staticmethod
    def _offer_preamble(
        memory: WorkflowMemory, appointment_type: AppointmentType, dropped_preference: bool
    ) -> str:
        if memory.get("practitioner_unmatched"):
            memory.set("practitioner_unmatched", False)
            return (
                "I couldn't find that clinician on our list, so here's the next "
                f"availability for a {appointment_type.display.lower()}:"
            )
        if dropped_preference:
            return (
                "They don't have anything free in that period, but here's what's "
                "available with our other clinicians:"
            )
        return f"I have these times for a {appointment_type.display.lower()}:"

    @staticmethod
    def _stored_offers(memory: WorkflowMemory) -> tuple[SlotOffer, ...]:
        return tuple(SlotOffer.model_validate(raw) for raw in memory.get("offers", []))

    def _memory(self, session: SessionState) -> WorkflowMemory:
        return WorkflowMemory(session.workflow_state, self.name)

    def _prompt_for_current_state(self, session: SessionState) -> WorkflowResponse:
        memory = self._memory(session)
        state = BookingState(memory.get("state", BookingState.COLLECTING_IDENTITY.value))
        prompts: dict[BookingState, tuple[str, AwaitedInput | None, WorkflowStatus]] = {
            BookingState.COLLECTING_IDENTITY: (
                "I can help with that. Could I take your full name and date of birth?",
                AwaitedInput.IDENTITY,
                WorkflowStatus.AWAITING_INPUT,
            ),
            BookingState.AWAITING_SECOND_FACTOR: (
                "Could I take the last four digits of the phone number on file?",
                AwaitedInput.SECOND_FACTOR,
                WorkflowStatus.AWAITING_INPUT,
            ),
            BookingState.COLLECTING_REASON: (
                "What would you like to be seen about?",
                AwaitedInput.REASON,
                WorkflowStatus.AWAITING_INPUT,
            ),
            BookingState.OFFERING_SLOTS: (
                "Which of those times works best?",
                AwaitedInput.SLOT_CHOICE,
                WorkflowStatus.AWAITING_INPUT,
            ),
            BookingState.CONFIRMING: (
                "Shall I book that for you?",
                AwaitedInput.CONFIRMATION,
                WorkflowStatus.AWAITING_INPUT,
            ),
            BookingState.BOOKED: (
                "You're all booked in. Is there anything else?",
                None,
                WorkflowStatus.COMPLETED,
            ),
            BookingState.ESCALATED: (
                "I'm passing you to a member of our staff.",
                None,
                WorkflowStatus.ESCALATED,
            ),
        }
        message, awaiting, status = prompts[state]
        return self._respond(
            session,
            state,
            status,
            message,
            awaiting=awaiting,
            offers=self._stored_offers(memory) if state is BookingState.OFFERING_SLOTS else (),
        )

    def _awaiting(
        self,
        session: SessionState,
        state: BookingState,
        message: str,
        awaiting: AwaitedInput,
    ) -> WorkflowResponse:
        self._memory(session).set("state", state.value)
        return self._respond(
            session, state, WorkflowStatus.AWAITING_INPUT, message, awaiting=awaiting
        )

    def _locked_out(self, session: SessionState, escalation_id: str | None) -> WorkflowResponse:
        self._memory(session).set("state", BookingState.ESCALATED.value)
        session.active_workflow = None
        return self._respond(
            session,
            BookingState.ESCALATED,
            WorkflowStatus.ESCALATED,
            "I haven't been able to confirm your identity, so I'm passing you to our "
            "front desk, who can help verify who you are.",
            escalation_id=escalation_id,
        )

    def _escalate(
        self,
        session: SessionState,
        turn: BookingInput,
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
            ai_action="No appointment booked",
        )
        self._memory(session).set("state", BookingState.ESCALATED.value)
        session.active_workflow = None
        return self._respond(
            session,
            BookingState.ESCALATED,
            WorkflowStatus.ESCALATED,
            message,
            escalation_id=escalation.escalation_id,
        )

    def _respond(
        self,
        session: SessionState,
        state: BookingState,
        status: WorkflowStatus,
        message: str,
        awaiting: AwaitedInput | None = None,
        offers: tuple[SlotOffer, ...] = (),
        appointment: object | None = None,
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
            appointment=appointment,  # type: ignore[arg-type]
            escalation_id=escalation_id,
        )
