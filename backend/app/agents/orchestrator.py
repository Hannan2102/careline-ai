"""The agent runtime.

One turn: **classify safety, then extract, then route, then execute, then
render, then record**. Safety runs first and unconditionally -- workflows are
reachable only from the branch where the decision is ALLOW, so there is no path
from an utterance to a record that skips it. That ordering is the whole reason
this class exists rather than each workflow being called directly.

Text and voice share this object (ADR 005). Voice adds STT before
:meth:`Orchestrator.handle_turn` and TTS after it, and nothing else changes.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

from app.agents.extraction import (
    ExtractedTurn,
    ExtractionContext,
    RuleBasedExtractor,
    TurnExtractor,
)
from app.agents.intents import Intent
from app.agents.state import SessionState
from app.agents.trace import StageTimings, TraceStore, TurnTrace
from app.ai.usage import UsageLedger
from app.config.settings import Settings, get_settings
from app.observability.logging import bind_trace, clear_trace, get_logger
from app.safety.models import SafetyOutcome
from app.schemas.domain import EscalationCategory
from app.services.audit_service import AuditService
from app.services.base import NotVerifiedError
from app.services.escalation_service import EscalationService
from app.services.medication_service import MedicationService
from app.services.refill_service import RefillService
from app.services.safety_service import SafetyService
from app.services.scheduling_service import SchedulingService
from app.services.verification_service import VerificationService
from app.workflows.appointment_management import (
    AppointmentManagementWorkflow,
    ManagementAction,
    ManagementInput,
)
from app.workflows.base import AwaitedInput, SlotOffer, WorkflowResponse
from app.workflows.clinic_faq import ClinicFaqInput, ClinicFaqWorkflow
from app.workflows.existing_patient_booking import (
    BookingInput,
    ExistingPatientBookingWorkflow,
)
from app.workflows.medication_lookup import MedicationLookupInput, MedicationLookupWorkflow
from app.workflows.refill_request import RefillRequestInput, RefillRequestWorkflow

logger = get_logger(__name__)

FALLBACK_MESSAGE = (
    "I can help with booking, changing or checking an appointment, what your "
    "prescription says, refill requests, and general questions about the clinic. "
    "Which of those would you like?"
)

TURN_LIMIT_MESSAGE = (
    "We've been talking for a while and I want to make sure you're looked after "
    "properly. Let me pass you to a member of our staff."
)

MANAGEMENT_INTENTS: dict[Intent, ManagementAction] = {
    Intent.LOOKUP_APPOINTMENT: ManagementAction.LOOKUP,
    Intent.CANCEL_APPOINTMENT: ManagementAction.CANCEL,
    Intent.RESCHEDULE_APPOINTMENT: ManagementAction.RESCHEDULE,
}


class TurnResult(BaseModel):
    """What the caller of the runtime gets back."""

    model_config = ConfigDict(frozen=True)

    message: str
    trace: TurnTrace

    @property
    def session_id(self) -> str:
        return self.trace.session_id


class Orchestrator:
    """Runs one conversational turn."""

    def __init__(
        self,
        safety: SafetyService,
        verification: VerificationService,
        scheduling: SchedulingService,
        medications: MedicationService,
        refills: RefillService,
        escalations: EscalationService,
        audit: AuditService | None = None,
        extractor: TurnExtractor | None = None,
        traces: TraceStore | None = None,
        ledger: UsageLedger | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.safety = safety
        self.escalations = escalations
        self.audit = audit or AuditService()
        self.extractor = extractor or RuleBasedExtractor()
        self.traces = traces or TraceStore()
        self.ledger = ledger
        self.settings = settings or get_settings()

        self.booking = ExistingPatientBookingWorkflow(
            verification, scheduling, escalations, self.audit
        )
        self.management = AppointmentManagementWorkflow(
            verification, scheduling, escalations, self.audit
        )
        self.lookup = MedicationLookupWorkflow(verification, medications, escalations, self.audit)
        self.refill = RefillRequestWorkflow(
            verification, medications, refills, escalations, self.audit
        )
        self.faq = ClinicFaqWorkflow(escalations)

    # ------------------------------------------------------------ one turn
    async def handle_turn(
        self, session: SessionState, utterance: str, now: datetime | None = None
    ) -> TurnResult:
        started = time.perf_counter()
        moment = now or datetime.now(UTC)
        turn_number = session.record_turn()
        audit_mark = len(self.audit.store.all())

        bind_trace(session_id=session.session_id, turn=turn_number)
        try:
            # 1. Safety, before anything else looks at the utterance.
            safety_started = time.perf_counter()
            evaluation = self.safety.evaluate(utterance, session)
            safety_ms = (time.perf_counter() - safety_started) * 1000

            if evaluation.decision.outcome is SafetyOutcome.REFUSE_AND_ESCALATE:
                message = evaluation.patient_message or FALLBACK_MESSAGE
                # A refusal ends whatever was in progress; the human takes over.
                session.active_workflow = None
                return self._record(
                    session,
                    turn_number,
                    utterance,
                    message,
                    evaluation,
                    ExtractedTurn(intent=Intent.UNKNOWN, confidence=1.0),
                    response=None,
                    escalation_id=(
                        evaluation.escalation.escalation_id if evaluation.escalation else None
                    ),
                    audit_mark=audit_mark,
                    timings=StageTimings(
                        safety_ms=safety_ms,
                        total_ms=(time.perf_counter() - started) * 1000,
                    ),
                    moment=moment,
                )

            if turn_number > self.settings.max_conversation_turns:
                return self._turn_limit_reached(
                    session, turn_number, utterance, evaluation, audit_mark, started, moment
                )

            # 2. Extraction, with the outstanding question as context.
            extraction_started = time.perf_counter()
            context = self._context(session)
            extracted = self.extractor.extract(utterance, context)
            extraction_ms = (time.perf_counter() - extraction_started) * 1000

            # 3. Route and execute.
            workflow_started = time.perf_counter()
            response = await self._route(session, extracted, context, utterance, moment)
            workflow_ms = (time.perf_counter() - workflow_started) * 1000

            message = response.message if response else FALLBACK_MESSAGE
            return self._record(
                session,
                turn_number,
                utterance,
                message,
                evaluation,
                extracted,
                response=response,
                escalation_id=response.escalation_id if response else None,
                audit_mark=audit_mark,
                timings=StageTimings(
                    safety_ms=safety_ms,
                    extraction_ms=extraction_ms,
                    workflow_ms=workflow_ms,
                    total_ms=(time.perf_counter() - started) * 1000,
                ),
                moment=moment,
            )
        finally:
            clear_trace()

    # -------------------------------------------------------------- routing
    async def _route(
        self,
        session: SessionState,
        extracted: ExtractedTurn,
        context: ExtractionContext,
        utterance: str,
        now: datetime,
    ) -> WorkflowResponse | None:
        """Continue the workflow in progress, or start the one the intent names."""
        active = session.active_workflow
        if active is not None:
            return await self._continue(session, active, extracted, utterance, now)

        match extracted.intent:
            case Intent.BOOK_APPOINTMENT:
                return await self.booking.advance(
                    session, self._booking_input(extracted, utterance), now=now
                )
            case (
                Intent.LOOKUP_APPOINTMENT
                | Intent.CANCEL_APPOINTMENT
                | Intent.RESCHEDULE_APPOINTMENT
            ):
                await self.management.start(session, MANAGEMENT_INTENTS[extracted.intent])
                return await self.management.advance(
                    session, self._management_input(extracted, utterance), now=now
                )
            case Intent.MEDICATION_LOOKUP:
                await self.lookup.start(session)
                return await self.lookup.advance(
                    session, self._lookup_input(extracted, utterance), now=now
                )
            case Intent.REFILL_REQUEST:
                await self.refill.start(session)
                return await self.refill.advance(
                    session, self._refill_input(extracted, utterance), now=now
                )
            case Intent.CLINIC_FAQ:
                return await self.faq.advance(
                    session, ClinicFaqInput(utterance=utterance, topic=extracted.faq_topic)
                )
            case _:
                return None

    async def _continue(
        self,
        session: SessionState,
        active: str,
        extracted: ExtractedTurn,
        utterance: str,
        now: datetime,
    ) -> WorkflowResponse | None:
        try:
            if active == self.booking.name:
                return await self.booking.advance(
                    session, self._booking_input(extracted, utterance), now=now
                )
            if active == self.management.name:
                return await self.management.advance(
                    session, self._management_input(extracted, utterance), now=now
                )
            if active == self.lookup.name:
                return await self.lookup.advance(
                    session, self._lookup_input(extracted, utterance), now=now
                )
            if active == self.refill.name:
                return await self.refill.advance(
                    session, self._refill_input(extracted, utterance), now=now
                )
        except NotVerifiedError:
            # Reached only if a workflow's own gate rejects a state we thought
            # was verified -- treat as a hard stop, not a retry loop.
            logger.warning("workflow_gate_denied", workflow=active)
            session.active_workflow = None
            return None
        return None

    # ------------------------------------------------------- input mapping
    @staticmethod
    def _booking_input(extracted: ExtractedTurn, utterance: str) -> BookingInput:
        return BookingInput(
            utterance=utterance,
            full_name=extracted.full_name,
            date_of_birth=extracted.date_of_birth,
            second_factor_value=extracted.second_factor_value,
            reason=extracted.reason,
            practitioner_name=extracted.practitioner_name,
            slot_choice=extracted.ordinal,
            none_suitable=extracted.none_suitable,
            confirm=extracted.confirm,
        )

    @staticmethod
    def _management_input(extracted: ExtractedTurn, utterance: str) -> ManagementInput:
        return ManagementInput(
            utterance=utterance,
            full_name=extracted.full_name,
            date_of_birth=extracted.date_of_birth,
            second_factor_value=extracted.second_factor_value,
            action=MANAGEMENT_INTENTS.get(extracted.intent),
            appointment_choice=extracted.ordinal,
            slot_choice=extracted.ordinal,
            none_suitable=extracted.none_suitable,
            confirm=extracted.confirm,
        )

    @staticmethod
    def _lookup_input(extracted: ExtractedTurn, utterance: str) -> MedicationLookupInput:
        return MedicationLookupInput(
            utterance=utterance,
            full_name=extracted.full_name,
            date_of_birth=extracted.date_of_birth,
            second_factor_value=extracted.second_factor_value,
            medication_name=extracted.medication_name,
            list_all=extracted.list_all,
        )

    @staticmethod
    def _refill_input(extracted: ExtractedTurn, utterance: str) -> RefillRequestInput:
        return RefillRequestInput(
            utterance=utterance,
            full_name=extracted.full_name,
            date_of_birth=extracted.date_of_birth,
            second_factor_value=extracted.second_factor_value,
            medication_name=extracted.medication_name,
            confirm=extracted.confirm,
        )

    # ------------------------------------------------------------- context
    def _context(self, session: SessionState) -> ExtractionContext:
        """What the last response left outstanding, so references resolve."""
        awaiting: AwaitedInput | None = None
        offers: tuple[SlotOffer, ...] = ()

        for workflow in (self.booking, self.management):
            raw = session.workflow_state.get(f"{workflow.name}.offers")
            if isinstance(raw, list) and raw:
                offers = tuple(SlotOffer.model_validate(item) for item in raw)

        last = session.workflow_state.get("_awaiting")
        if isinstance(last, str):
            awaiting = AwaitedInput(last)
        return ExtractionContext(awaiting=awaiting, offers=offers)

    # -------------------------------------------------------------- record
    def _record(
        self,
        session: SessionState,
        turn_number: int,
        utterance: str,
        message: str,
        evaluation: object,
        extracted: ExtractedTurn,
        response: WorkflowResponse | None,
        escalation_id: str | None,
        audit_mark: int,
        timings: StageTimings,
        moment: datetime,
    ) -> TurnResult:
        decision = evaluation.decision  # type: ignore[attr-defined]

        # Remember what we just asked for, so the next turn's extraction knows
        # whether a bare "John Smith" is a name or a doctor's surname.
        if response is not None and response.awaiting is not None:
            session.workflow_state["_awaiting"] = response.awaiting.value
        else:
            session.workflow_state.pop("_awaiting", None)

        operations = tuple(self.audit.store.all()[audit_mark:])
        trace = TurnTrace(
            session_id=session.session_id,
            turn_number=turn_number,
            created_at=moment,
            utterance=utterance,
            response=message,
            safety_outcome=decision.outcome,
            safety_category=decision.category,
            safety_rule=decision.matched_rule,
            intent=extracted.intent,
            confidence=extracted.confidence,
            entities=self._entities(extracted),
            workflow=response.workflow if response else None,
            workflow_state=response.state if response else None,
            workflow_status=response.status.value if response else None,
            operations=operations,
            escalation_id=escalation_id,
            verification_state=session.verification.value,
            timings=timings,
            estimated_cost_usd=str(
                self.ledger.session_total(session.session_id) if self.ledger else 0
            ),
        )
        self.traces.append(trace)
        logger.info(
            "turn_completed",
            turn=turn_number,
            intent=trace.intent.value,
            workflow=trace.workflow,
            safety=trace.safety_outcome.value,
            total_ms=round(timings.total_ms, 1),
        )
        return TurnResult(message=message, trace=trace)

    @staticmethod
    def _entities(extracted: ExtractedTurn) -> dict[str, str]:
        """Entity summary for the trace. Names and dates are deliberately absent.

        The trace is a debugging record, and it should not become a second
        place patient identifiers accumulate.
        """
        found: dict[str, str] = {}
        if extracted.full_name:
            found["full_name"] = "<redacted>"
        if extracted.date_of_birth:
            found["date_of_birth"] = "<redacted>"
        if extracted.second_factor_value:
            found["second_factor"] = "<redacted>"
        if extracted.medication_name:
            found["medication_name"] = extracted.medication_name
        if extracted.practitioner_name:
            found["practitioner_name"] = extracted.practitioner_name
        if extracted.faq_topic:
            found["faq_topic"] = extracted.faq_topic
        if extracted.ordinal is not None:
            found["ordinal"] = str(extracted.ordinal)
        if extracted.confirm is not None:
            found["confirm"] = str(extracted.confirm)
        return found

    def _turn_limit_reached(
        self,
        session: SessionState,
        turn_number: int,
        utterance: str,
        evaluation: object,
        audit_mark: int,
        started: float,
        moment: datetime,
    ) -> TurnResult:
        """Hand over rather than loop. Also a cost ceiling (COSTS.md)."""
        escalation = self.escalations.create(
            category=EscalationCategory.SYSTEM_UNCERTAINTY,
            summary=(
                f"Conversation reached the {self.settings.max_conversation_turns}-turn limit."
            ),
            session_id=session.session_id,
            patient_ref=session.patient_ref,
            verification_state=session.verification,
            patient_question=utterance,
            ai_action="Conversation handed over at the turn limit",
        )
        session.active_workflow = None
        return self._record(
            session,
            turn_number,
            utterance,
            TURN_LIMIT_MESSAGE,
            evaluation,
            ExtractedTurn(intent=Intent.UNKNOWN, confidence=1.0),
            response=None,
            escalation_id=escalation.escalation_id,
            audit_mark=audit_mark,
            timings=StageTimings(total_ms=(time.perf_counter() - started) * 1000),
            moment=moment,
        )
