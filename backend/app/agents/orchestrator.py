"""The agent runtime.

One turn: **classify safety, then extract, then route, then execute, then
render, then record**. Safety runs first and unconditionally -- workflows are
reachable only from the branch where the decision is ALLOW, so there is no path
from an utterance to a record that skips it. That ordering is the whole reason
this class exists rather than each workflow being called directly.

Text and voice share this object (ADR 005). Voice adds STT before
:meth:`Orchestrator.handle_turn` and TTS after it, and nothing else changes.

Every call opens with identity (ADR 010). Until the caller is verified, each
turn that safety allows goes to the shared identity steps -- name spelled back,
date of birth read back, then the record -- and whatever the caller asked for
on the way is remembered and acted on the moment they are verified. That is
the orchestrator's job rather than each workflow's, so there is exactly one
way into a verified session, whichever entry point opened the call.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

from app.agents.extraction import (
    ExtractedTurn,
    ExtractionContext,
    RuleBasedExtractor,
    TurnExtractor,
    asks_to_book,
    spoken_digits,
)
from app.agents.intents import Intent
from app.agents.state import SessionState
from app.agents.trace import StageTimings, TraceStore, TurnTrace
from app.ai.usage import UsageLedger
from app.config.clinic import CLINIC_NAME, GREETING
from app.config.settings import Settings, get_settings
from app.observability.logging import bind_trace, clear_trace, get_logger
from app.safety.models import SafetyOutcome
from app.schemas.domain import AppointmentType, AuditAction, EscalationCategory
from app.services.audit_service import AuditService
from app.services.base import NotVerifiedError
from app.services.coverage_service import CoverageService
from app.services.escalation_service import EscalationService
from app.services.medication_service import MedicationService
from app.services.persistence_service import PersistenceService
from app.services.refill_service import RefillService
from app.services.safety_service import SafetyService
from app.services.scheduling_service import SchedulingService
from app.services.verification_service import VerificationService
from app.workflows.appointment_management import (
    AppointmentManagementWorkflow,
    ManagementAction,
    ManagementInput,
)
from app.workflows.base import (
    AwaitedInput,
    Offer,
    SlotOffer,
    WorkflowMemory,
    WorkflowResponse,
    WorkflowStatus,
)
from app.workflows.clinic_faq import ClinicFaqInput, ClinicFaqWorkflow
from app.workflows.coverage_lookup import CoverageLookupInput, CoverageLookupWorkflow
from app.workflows.existing_patient_booking import (
    BookingInput,
    ExistingPatientBookingWorkflow,
)
from app.workflows.identity import (
    ASK_NAME_FOR_REQUEST,
    IDENTITY_HANDOVER,
    LOCKED_OUT,
    IdentityCollector,
    IdentityInput,
    IdentityOutcome,
    IdentityResult,
    IdentityStep,
)
from app.workflows.medication_lookup import MedicationLookupInput, MedicationLookupWorkflow
from app.workflows.new_patient import NewPatientInput, NewPatientWorkflow
from app.workflows.refill_request import RefillRequestInput, RefillRequestWorkflow

logger = get_logger(__name__)


def _carried_something(extracted: ExtractedTurn) -> bool:
    """Whether the turn yielded anything the agent recognised."""
    return any(
        (
            extracted.full_name,
            extracted.date_of_birth,
            extracted.dob_candidates,
            extracted.spelled,
            extracted.second_factor_value,
            extracted.medication_name,
            extracted.practitioner_name,
            extracted.faq_topic,
            extracted.ordinal,
            extracted.confirm is not None,
        )
    )


@dataclass(frozen=True)
class _Direct:
    """A reply from the orchestrator itself, with no workflow behind it."""

    message: str
    escalation_id: str | None = None


#: Where a pending yes-or-no is kept between turns.
PENDING_OFFER = "_offer"

#: What the trace calls the identity steps, in the workflow column.
IDENTITY_WORKFLOW = "identity"

#: What the caller asked for before they were verified, kept until they are.
#: Under the identity namespace, so it goes when the conversation does.
PENDING_INTENT = "_identity.pending_intent"

#: Said once verified, when nothing was asked for on the way.
HOW_CAN_I_HELP = "How can I help you today?"

#: Whether the caller is already a patient: "existing" or "new". The first
#: thing every call settles, because the answer chooses between verifying
#: them against the record and registering them.
PATIENT_STATUS = "_identity.patient_status"
_STATUS_REASKED = "_identity.status_reasked"

ASK_STATUS_FOR_REQUEST = (
    "I can help with that. First, are you an existing patient, or are you new and "
    "would like to register?"
)
#: Asked again as a yes-or-no, which is easier to answer than the either-or.
ASK_STATUS_AGAIN = "Sorry — have you been a patient with us before?"
ASK_NAME_EXISTING = "Great. Could I take your first and last name?"

#: Requests only somebody already on the record can make, so saying one
#: answers the opening question: nobody new has an appointment to cancel.
_EXISTING_PATIENT_INTENTS = frozenset(
    {
        Intent.LOOKUP_APPOINTMENT,
        Intent.CANCEL_APPOINTMENT,
        Intent.RESCHEDULE_APPOINTMENT,
        Intent.MEDICATION_LOOKUP,
        Intent.REFILL_REQUEST,
        Intent.COVERAGE_LOOKUP,
    }
)

#: Where the first part of a number read out in pieces waits for the rest.
DICTATION_CARRY = "_dictation_carry"

#: How many digits a complete answer has, for the questions that take digits.
_DIGITS_WANTED = {AwaitedInput.PHONE: 10, AwaitedInput.SECOND_FACTOR: 4}

#: Said when part of a number has arrived and the rest has not. Short, so it
#: does not talk over a caller who was only drawing breath.
GO_ON = "Mm-hm, go on."

#: An unverified caller who wants more after a clinic question (the
#: ``identity_first_allow_faq`` path): anything further needs a name.
ASK_NAME_FOR_MORE = "Of course. First, could I take your first and last name?"

#: Said when the request is a real one the agent has no way to serve.
#:
#: Not the capability menu. Reading the menu to somebody who has just asked
#: for something specific answers a question they did not ask and leaves them
#: with nowhere to go; the honest reply is that this is not something the
#: agent can do, followed by the thing it can always do.
OUT_OF_SCOPE_MESSAGE = (
    "That isn't something I'm able to help with myself. "
    "I can pass you to a member of our staff who can — would you like me to?"
)

DECLINED_MESSAGE = "No problem. Is there anything else I can help with?"

#: Asked at the end of anything the agent finished doing.
ANYTHING_ELSE = "Is there anything else I can help with?"

#: And what to say when the answer is no.
#:
#: The call ends here. A request finished, nothing else wanted, and a line left
#: open after that is one the caller has to work out how to leave -- which over
#: the phone means sitting through "are you still there?" and then being hung
#: up on for silence.
GOODBYE_MESSAGE = f"Thanks for calling {CLINIC_NAME}. Take care."

#: Said when they do want something else, in place of the capability menu:
#: they have just used the agent, so they know what it does.
MORE_HELP_MESSAGE = "Of course. What can I do for you?"

HANDOVER_MESSAGE = (
    "Of course — I'll pass you to a member of our staff. I've made a note of what "
    "we've discussed so you won't need to start over."
)

#: What to say when the agent has asked the same thing twice and got nowhere.
#:
#: Honest about whose problem it is. A caller who has now heard one question
#: three times knows perfectly well that something is wrong, and a fourth
#: identical sentence is the point at which a real person hangs up.
STUCK_MESSAGE = (
    "I'm sorry — I don't seem to be getting this right. "
    "Would you like me to pass you to a member of our staff?"
)

#: How many times in a row the agent may say the same sentence.
#:
#: Twice. Once is a re-ask, which is normal and often works; a third is a
#: loop, whatever caused it. This is deliberately a property of the reply
#: rather than of any workflow, so it catches the loops nobody has found yet.
MAX_IDENTICAL_REPLIES = 2

#: Where the last reply and its repeat count are kept.
LAST_REPLY = "_last_reply"
REPLY_REPEATS = "_reply_repeats"

#: How many turns the agent may fail to understand before offering a person.
#:
#: Two, because the first is often a greeting or a false start and the menu is
#: a fair answer to those. A third recital of the same list is not.
REPEATS_BEFORE_OFFERING_A_PERSON = 2

#: Where the count of consecutive turns nothing could answer is kept.
UNKNOWN_STREAK = "_unknown_streak"

FALLBACK_MESSAGE = (
    "I can help with booking, changing or checking an appointment, what your "
    "prescription says, refill requests, and general questions about the clinic. "
    "Which of those would you like?"
)

TURN_LIMIT_MESSAGE = (
    "We've been talking for a while and I want to make sure you're looked after "
    "properly. Let me pass you to a member of our staff."
)

#: Questions whose answer is read out a piece at a time.
_DICTATION = frozenset(
    {
        AwaitedInput.NAME_SPELLING,
        AwaitedInput.NAME_CONFIRMATION,
        AwaitedInput.DATE_OF_BIRTH,
        AwaitedInput.DOB_CONFIRMATION,
        AwaitedInput.SECOND_FACTOR,
        AwaitedInput.PHONE,
    }
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
        coverage: CoverageService,
        escalations: EscalationService,
        audit: AuditService | None = None,
        extractor: TurnExtractor | None = None,
        traces: TraceStore | None = None,
        ledger: UsageLedger | None = None,
        settings: Settings | None = None,
        persistence: PersistenceService | None = None,
    ) -> None:
        self.safety = safety
        self.escalations = escalations
        self.audit = audit or AuditService()
        self.extractor = extractor or RuleBasedExtractor()
        self.traces = traces or TraceStore()
        self.ledger = ledger
        self.settings = settings or get_settings()
        self.persistence = persistence

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
        self.coverage = CoverageLookupWorkflow(verification, coverage, escalations, self.audit)
        self.registration = NewPatientWorkflow(
            # The verification service already owns the patient service, and a
            # second reference passed in beside it could drift to a different
            # EHR provider than the one identity is checked against.
            verification.patients,
            verification,
            escalations,
            self.audit,
        )
        self.faq = ClinicFaqWorkflow(escalations)
        self.identity = IdentityCollector(verification, self.audit)

    # ---------------------------------------------------------- the opening
    def greet(self, session: SessionState) -> str:
        """What the caller hears before saying anything, and what it asked.

        Every entry point opens a call with this -- voice speaks it, the CLI
        prints it, the HTTP API returns it with the new session -- so the first
        turn is an answer to "could I take your first and last name?" however
        the call was started. Recording that here is belt and braces:
        ``_context`` would derive it anyway for an unverified session.
        """
        if self._identity_pending(session):
            session.workflow_state["_awaiting"] = AwaitedInput.PATIENT_STATUS.value
        return GREETING

    def expects_dictation(self, session: SessionState) -> bool:
        """Whether the caller is being asked to read something out piece by piece.

        Letters of a name, digits of a date or a phone number. The voice layer
        waits longer for these before answering, because callers pause between
        the pieces and the recogniser reports every pause as the end of what
        they said. It asks here rather than reading session state itself:
        which questions are dictation is a fact about the conversation, and
        the turn manager is only allowed to know about timing.
        """
        return self._context(session).awaiting in _DICTATION

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
                return await self._record(
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
                return await self._turn_limit_reached(
                    session, turn_number, utterance, evaluation, audit_mark, started, moment
                )

            # 2. Extraction, with the outstanding question as context.
            extraction_started = time.perf_counter()
            context = self._context(session)
            heard = self._with_carry(session, utterance, context)
            extracted = await self.extractor.aextract(heard, context)
            extraction_ms = (time.perf_counter() - extraction_started) * 1000

            # 3. Route and execute.
            workflow_started = time.perf_counter()
            partial = (
                self._hold_partial_number(session, heard, context)
                if spoken_digits(utterance)
                else None
            )
            response, direct = partial or await self._answer(
                session, extracted, context, heard, moment
            )
            workflow_ms = (time.perf_counter() - workflow_started) * 1000

            message = self._break_a_loop(session, self._offer_more_help(session, response, direct))
            return await self._record(
                session,
                turn_number,
                utterance,
                message,
                evaluation,
                extracted,
                response=response,
                escalation_id=(response.escalation_id if response else direct.escalation_id),
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
    async def _answer(
        self,
        session: SessionState,
        extracted: ExtractedTurn,
        context: ExtractionContext,
        utterance: str,
        now: datetime,
    ) -> tuple[WorkflowResponse | None, _Direct]:
        """The reply, from a workflow or from the orchestrator itself.

        Three things can answer a turn. A workflow in progress or one the
        intent names; a yes or no to something the agent offered last turn;
        and -- when nothing else can -- the orchestrator, which says so and
        offers a person rather than reciting the menu at somebody for the
        second time.
        """
        gated = await self._identity_gate(session, extracted, utterance, now)
        if gated is not None:
            return gated

        answered = await self._answer_an_offer(session, extracted, utterance, now)
        if answered is not None:
            return answered

        response = await self._route(session, extracted, context, utterance, now)
        if response is not None:
            session.workflow_state.pop(UNKNOWN_STREAK, None)
            if self.registration.just_registered(response):
                # Registered. If they said what they wanted before they gave
                # their details, do that now; otherwise the registration's own
                # reply already says what the agent can do for them.
                resumed = await self._resume_held_request(
                    session, self.registration.registered_line(session), now
                )
                if resumed is not None:
                    return resumed, _Direct(FALLBACK_MESSAGE)
            return response, _Direct(FALLBACK_MESSAGE)
        return None, self._nothing_matched(session, extracted)

    async def _answer_an_offer(
        self,
        session: SessionState,
        extracted: ExtractedTurn,
        utterance: str,
        now: datetime,
    ) -> tuple[WorkflowResponse | None, _Direct] | None:
        """Honour a yes or no to what the agent offered on the last turn.

        ``None`` when there is nothing outstanding, or when the caller
        answered with something other than yes or no -- which is not a
        refusal, it is a change of subject, and the turn belongs to whatever
        they actually said.
        """
        pending = session.workflow_state.pop(PENDING_OFFER, None)
        if not isinstance(pending, str) or extracted.confirm is None:
            return None

        offer = Offer(pending)
        if offer is Offer.ANYTHING_ELSE:
            if extracted.confirm is False:
                logger.info("call_completed", session_id=session.session_id)
                session.end(now)
                return None, _Direct(GOODBYE_MESSAGE)
            return None, _Direct(MORE_HELP_MESSAGE)

        if extracted.confirm is False:
            return None, _Direct(DECLINED_MESSAGE)

        if offer is Offer.BOOK_APPOINTMENT:
            return (
                await self.booking.advance(
                    session, self._booking_input(extracted, utterance), now=now
                ),
                _Direct(FALLBACK_MESSAGE),
            )
        return None, self._hand_over(session, utterance)

    # ------------------------------------------------------ split numbers
    @staticmethod
    def _with_carry(session: SessionState, utterance: str, context: ExtractionContext) -> str:
        """This turn, with the start of a number from the turn before.

        Found on a live call: the caller said "One", paused, then read out the
        other nine digits. The recogniser made two turns of it, neither held
        ten digits, so neither was a phone number -- and after two identical
        re-asks the agent offered to pass the caller to a person for reading
        out their own phone number.
        """
        carried = session.workflow_state.pop(DICTATION_CARRY, None)
        if isinstance(carried, str) and context.awaiting in _DIGITS_WANTED:
            return f"{carried} {utterance}"
        return utterance

    @staticmethod
    def _hold_partial_number(
        session: SessionState, heard: str, context: ExtractionContext
    ) -> tuple[WorkflowResponse | None, _Direct] | None:
        """Keep the first part of a number and wait for the rest.

        Only for a question that takes a fixed number of digits, and only while
        fewer have arrived than it needs. Nothing is checked or written on a
        partial number; the next turn's digits are joined on and the whole is
        read as one answer. A turn that adds no digits is never held -- "that's
        all of it" after three digits goes to the workflow, which asks for the
        number again, rather than to "go on" for ever.
        """
        wanted = _DIGITS_WANTED.get(context.awaiting) if context.awaiting else None
        if wanted is None:
            return None
        found = len(spoken_digits(heard))
        if found == 0 or found >= wanted:
            return None
        session.workflow_state[DICTATION_CARRY] = heard
        assert context.awaiting is not None
        return (
            WorkflowResponse(
                workflow=session.active_workflow or IDENTITY_WORKFLOW,
                state="DICTATING",
                status=WorkflowStatus.AWAITING_INPUT,
                message=GO_ON,
                awaiting=context.awaiting,
            ),
            _Direct(GO_ON),
        )

    # ------------------------------------------------------------- identity
    def _identity_pending(self, session: SessionState) -> bool:
        """Whether this turn belongs to the identity steps.

        Every unverified call, except a registration -- which takes the same
        steps itself, through the same collector -- and a call already on its
        way to a person.
        """
        if session.is_verified or session.is_locked_out or not session.is_active:
            return False
        if session.active_workflow == self.registration.name:
            return False
        return self.identity.step(session) not in (
            IdentityStep.HANDED_OVER,
            IdentityStep.LOCKED_OUT,
            IdentityStep.VERIFIED,
        )

    async def _identity_gate(
        self,
        session: SessionState,
        extracted: ExtractedTurn,
        utterance: str,
        now: datetime,
    ) -> tuple[WorkflowResponse | None, _Direct] | None:
        """Take the caller's identity before anything else (ADR 010).

        ``None`` hands the turn on to ordinary routing: a verified caller, a
        caller who has said they are new (registration collects the same
        details through the same steps), a clinic question when
        ``identity_first_allow_faq`` allows one, and the yes or no to an offer
        the agent made.
        """
        state = session.workflow_state

        if not self._identity_pending(session):
            if session.is_verified or extracted.intent in (Intent.CLINIC_FAQ, Intent.NEW_PATIENT):
                return None
            if session.is_locked_out:
                return None, _Direct(LOCKED_OUT)
            if self.identity.step(session) is IdentityStep.HANDED_OVER:
                # Already on the way to a person. Nothing behind the gate is
                # reachable from here, and asking for a name a fourth time is
                # the loop the handover was for.
                return None, _Direct(IDENTITY_HANDOVER)
            return None

        if extracted.intent is Intent.NEW_PATIENT:
            # The caller has to say this (ADR 009). What they have already
            # confirmed comes with them: registration reads the same steps.
            state[PATIENT_STATUS] = "new"
            if asks_to_book(utterance) and PENDING_INTENT not in state:
                # "...can I get an appointment?" -- booked once registered.
                state[PENDING_INTENT] = {"intent": Intent.BOOK_APPOINTMENT.value}
            return None

        step = self.identity.step(session)
        if (
            self.settings.identity_first_allow_faq
            and extracted.intent is Intent.CLINIC_FAQ
            and step is IdentityStep.ASKING_NAME
            and not self.identity.has_started(session)
        ):
            return None

        offer = state.get(PENDING_OFFER)
        if offer is not None and extracted.confirm is not None:
            if offer == Offer.ANYTHING_ELSE.value and extracted.confirm is True:
                state.pop(PENDING_OFFER, None)
                return self._identity_reply(
                    session,
                    IdentityResult(
                        outcome=IdentityOutcome.NEEDS_IDENTITY,
                        message=ASK_NAME_FOR_MORE,
                        awaiting=AwaitedInput.NAME,
                        step=IdentityStep.ASKING_NAME,
                    ),
                )
            # "No, that's everything", or yes to a person: the offer's to answer.
            return None
        state.pop(PENDING_OFFER, None)

        if state.get(PATIENT_STATUS) is None and not self.identity.has_started(session):
            settled = await self._settle_patient_status(session, extracted, utterance, now)
            if settled is not None:
                return settled

        request_noted = self._note_request(session, extracted, step)
        result = await self.identity.advance(
            session, self._identity_input(extracted, utterance, request_noted=request_noted)
        )
        if result.outcome is IdentityOutcome.VERIFIED:
            return await self._resume_after_verification(session, result, now)
        return self._identity_reply(session, result)

    async def _settle_patient_status(
        self,
        session: SessionState,
        extracted: ExtractedTurn,
        utterance: str,
        now: datetime,
    ) -> tuple[WorkflowResponse | None, _Direct] | None:
        """Answer the opening question: existing patient, or new?

        Settled by what the caller said, in this order: saying so; giving a
        name, which only somebody expecting to be looked up does; asking for
        something only a patient can have, like an appointment to cancel. A
        new patient goes straight to registration, where the same identity
        steps take their details. ``None`` means an existing patient whose
        turn should now go on to the identity steps.
        """
        state = session.workflow_state
        status = extracted.patient_status
        if status is None and extracted.full_name is not None:
            status = "existing"
        if status is None and state.get(_STATUS_REASKED) and extracted.confirm is not None:
            # The re-ask is "have you been a patient with us before?"
            status = "existing" if extracted.confirm else "new"
        if status is None and extracted.intent in _EXISTING_PATIENT_INTENTS:
            status = "existing"

        noted = self._note_request(session, extracted, IdentityStep.ASKING_NAME)

        if status == "new":
            state[PATIENT_STATUS] = "new"
            await self.registration.start(session)
            return (
                await self.registration.advance(
                    session, self._registration_input(extracted, utterance), now=now
                ),
                _Direct(FALLBACK_MESSAGE),
            )

        if status is None:
            first_time = not state.get(_STATUS_REASKED)
            state[_STATUS_REASKED] = True
            message = ASK_STATUS_FOR_REQUEST if noted and first_time else ASK_STATUS_AGAIN
            return self._identity_reply(
                session,
                IdentityResult(
                    outcome=IdentityOutcome.NEEDS_IDENTITY,
                    message=message,
                    awaiting=AwaitedInput.PATIENT_STATUS,
                    step=IdentityStep.ASKING_STATUS,
                ),
            )

        state[PATIENT_STATUS] = "existing"
        if extracted.full_name is None and extracted.date_of_birth is None:
            return self._identity_reply(
                session,
                IdentityResult(
                    outcome=IdentityOutcome.NEEDS_IDENTITY,
                    message=ASK_NAME_FOR_REQUEST if noted else ASK_NAME_EXISTING,
                    awaiting=AwaitedInput.NAME,
                    step=IdentityStep.ASKING_NAME,
                ),
            )
        return None

    @staticmethod
    def _note_request(session: SessionState, extracted: ExtractedTurn, step: IdentityStep) -> bool:
        """Remember what the caller wants, to act on once they are verified.

        "I need to reschedule, my name is John Smith" is two answers in one
        breath, and the second is the reason for the call. Only the first
        request is kept, and only while the name is being asked for: after
        that, every turn is an answer to an identity question.
        """
        if step is not IdentityStep.ASKING_NAME or extracted.intent in (
            Intent.UNKNOWN,
            Intent.NEW_PATIENT,
        ):
            return False
        if PENDING_INTENT in session.workflow_state:
            return False
        session.workflow_state[PENDING_INTENT] = {
            "intent": extracted.intent.value,
            "reason": extracted.reason,
            "practitioner_name": extracted.practitioner_name,
            "medication_name": extracted.medication_name,
            "faq_topic": extracted.faq_topic,
            "list_all": extracted.list_all,
        }
        logger.info(
            "request_held_for_identity",
            session_id=session.session_id,
            intent=extracted.intent.value,
        )
        return extracted.full_name is None

    async def _resume_after_verification(
        self, session: SessionState, result: IdentityResult, now: datetime
    ) -> tuple[WorkflowResponse | None, _Direct]:
        """Verified: do what they rang about, or ask what that is."""
        resumed = await self._resume_held_request(session, result.message, now)
        if resumed is not None:
            return resumed, _Direct(FALLBACK_MESSAGE)
        return self._identity_reply(
            session,
            result.model_copy(update={"message": f"{result.message} {HOW_CAN_I_HELP}".strip()}),
        )

    async def _resume_held_request(
        self, session: SessionState, prefix: str, now: datetime
    ) -> WorkflowResponse | None:
        """Act on the request made before the caller was verified or registered."""
        held = session.workflow_state.pop(PENDING_INTENT, None)
        if not isinstance(held, dict):
            return None
        try:
            intent = Intent(str(held.get("intent")))
        except ValueError:
            return None

        def text(key: str) -> str | None:
            value = held.get(key)
            return value if isinstance(value, str) else None

        resumed = ExtractedTurn(
            intent=intent,
            confidence=0.9,
            reason=text("reason"),
            practitioner_name=text("practitioner_name"),
            medication_name=text("medication_name"),
            faq_topic=text("faq_topic"),
            list_all=bool(held.get("list_all")),
        )
        response = await self._route(session, resumed, ExtractionContext(), "", now)
        if response is None:
            return None
        return response.model_copy(update={"message": f"{prefix} {response.message}".strip()})

    @staticmethod
    def _identity_reply(
        session: SessionState, result: IdentityResult
    ) -> tuple[WorkflowResponse | None, _Direct]:
        """The identity steps' reply, shaped like a workflow's so the trace shows it."""
        ended = result.outcome in (IdentityOutcome.HANDED_OVER, IdentityOutcome.LOCKED_OUT)
        if ended:
            session.active_workflow = None
        step = result.step or IdentityStep.ASKING_NAME
        return (
            WorkflowResponse(
                workflow=IDENTITY_WORKFLOW,
                state=step.value,
                status=WorkflowStatus.ESCALATED if ended else WorkflowStatus.AWAITING_INPUT,
                message=result.message,
                awaiting=None if ended else result.awaiting,
                escalation_id=result.escalation_id,
            ),
            _Direct(result.message, escalation_id=result.escalation_id),
        )

    @staticmethod
    def _identity_input(
        extracted: ExtractedTurn, utterance: str, *, request_noted: bool = False
    ) -> IdentityInput:
        return IdentityInput(
            utterance=utterance,
            full_name=extracted.full_name,
            spelled=extracted.spelled,
            date_of_birth=extracted.date_of_birth,
            dob_candidates=extracted.dob_candidates,
            confirm=extracted.confirm,
            second_factor_value=extracted.second_factor_value,
            request_noted=request_noted,
        )

    @staticmethod
    def _offer_more_help(
        session: SessionState, response: WorkflowResponse | None, direct: _Direct
    ) -> str:
        """End a finished request by asking whether there is another one.

        Here rather than in each workflow, for two reasons. Every completion
        should ask it and several did not -- a medication lookup read the
        dosage back and stopped, leaving the caller to work out whether the
        agent was still listening. And the *answer* has to live somewhere
        central anyway: "no, that's everything" belongs to no workflow, which
        is why it used to reach the capability menu.

        Left alone when the workflow is escalating (the call is on its way to
        a person), when it has already asked something of its own, and when
        the message is a question already.
        """
        if response is None:
            return direct.message
        if response.status is not WorkflowStatus.COMPLETED or response.offer is not None:
            return response.message

        session.workflow_state[PENDING_OFFER] = Offer.ANYTHING_ELSE.value
        message = response.message.strip()
        if message.endswith("?"):
            return message
        return f"{message} {ANYTHING_ELSE}"

    def _break_a_loop(self, session: SessionState, message: str) -> str:
        """Offer a person rather than say the same sentence a third time.

        The last line of defence, and the only one that does not need to know
        why. Every loop found on a live call so far -- a workflow left in a
        finished state, a choice nothing could resolve, a question whose
        answer the rules could not parse -- looked identical from the caller's
        side: the same sentence, again. So this watches the sentences.

        It does not end the workflow. The caller may still answer the question
        on the next turn, or say no and carry on; what changes is that a way
        out has been offered.
        """
        if message == GO_ON:
            # Not a loop: each one is a different piece of the same number.
            return message
        last = session.workflow_state.get(LAST_REPLY)
        seen = session.workflow_state.get(REPLY_REPEATS, 0)
        repeats = (seen if isinstance(seen, int) else 0) + 1 if message == last else 1
        session.workflow_state[LAST_REPLY] = message
        session.workflow_state[REPLY_REPEATS] = repeats

        if repeats <= MAX_IDENTICAL_REPLIES:
            return message

        logger.warning(
            "conversation_stuck",
            session_id=session.session_id,
            repeated=repeats,
            workflow=session.active_workflow,
        )
        session.workflow_state[PENDING_OFFER] = Offer.HUMAN.value
        session.workflow_state[REPLY_REPEATS] = 0
        session.workflow_state[LAST_REPLY] = STUCK_MESSAGE
        return STUCK_MESSAGE

    def _nothing_matched(self, session: SessionState, extracted: ExtractedTurn) -> _Direct:
        """What to say when no workflow claimed the turn.

        The menu is a reasonable first answer to "hello?" and a poor second
        answer to anything. A caller who has asked twice for something we do
        not do is not going to be helped by hearing the list again, and a
        request the classifier recognised as out of scope never needed the
        list at all -- both get offered a person, which is the one thing that
        is always true.
        """
        seen = session.workflow_state.get(UNKNOWN_STREAK, 0)
        # A turn we got something out of is not a turn we failed to
        # understand. "John Smith, born the fifteenth of February" reaches
        # here when no workflow is waiting for it -- understood perfectly,
        # merely unexpected -- and telling that caller we cannot help them is
        # both wrong and alarming. It counts as progress, not a strike.
        if _carried_something(extracted):
            session.workflow_state.pop(UNKNOWN_STREAK, None)
            return _Direct(FALLBACK_MESSAGE)

        streak = (seen if isinstance(seen, int) else 0) + 1
        session.workflow_state[UNKNOWN_STREAK] = streak
        if extracted.out_of_scope or streak >= REPEATS_BEFORE_OFFERING_A_PERSON:
            session.workflow_state[PENDING_OFFER] = Offer.HUMAN.value
            return _Direct(OUT_OF_SCOPE_MESSAGE)
        return _Direct(FALLBACK_MESSAGE)

    def _hand_over(self, session: SessionState, utterance: str) -> _Direct:
        """Escalate at the caller's word, once they have said yes to it."""
        escalation = self.escalations.create(
            category=EscalationCategory.ADMINISTRATIVE,
            summary="Request outside what the agent can do; caller accepted a handover.",
            session_id=session.session_id,
            patient_ref=session.patient_ref,
            verification_state=session.verification,
            patient_question=utterance or None,
            ai_action="Offered a member of staff, which the caller accepted",
        )
        self.audit.record(
            AuditAction.ESCALATION_CREATED,
            session_id=session.session_id,
            patient_ref=session.patient_ref,
            resource_type="Escalation",
            resource_id=escalation.escalation_id,
        )
        session.workflow_state.pop(UNKNOWN_STREAK, None)
        return _Direct(HANDOVER_MESSAGE, escalation_id=escalation.escalation_id)

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
        if active is not None and not self._changed_subject(session, extracted, active):
            return await self._continue(session, active, extracted, utterance, now)

        match extracted.intent:
            case Intent.BOOK_APPOINTMENT:
                if self.registration.first_visit_pending(session):
                    # Registered this call: their first appointment is the
                    # long one, whatever they say it is for.
                    await self.booking.start(session, appointment_type=AppointmentType.NEW_PATIENT)
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
            case Intent.NEW_PATIENT:
                await self.registration.start(session)
                return await self.registration.advance(
                    session, self._registration_input(extracted, utterance), now=now
                )
            case Intent.MEDICATION_LOOKUP:
                await self.lookup.start(session)
                return await self.lookup.advance(
                    session, self._lookup_input(extracted, utterance), now=now
                )
            case Intent.COVERAGE_LOOKUP:
                await self.coverage.start(session)
                return await self.coverage.advance(
                    session, self._coverage_input(extracted, utterance), now=now
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

    def _changed_subject(
        self, session: SessionState, extracted: ExtractedTurn, active: str
    ) -> bool:
        """Whether to put down what we were doing and pick up something else.

        Callers do this constantly -- "actually, could you sort out my repeat
        while I'm on?" -- and a workflow that owns every turn until it finishes
        answers that with the question it asked before, forever.

        Deliberately narrow. It requires the model to have said the caller
        moved on *and* to have named a different workflow: the rules cannot
        reach this, because telling a change of subject from a clumsy answer
        is exactly the judgement they are bad at, and being wrong here throws
        away a booking that was half made.

        What is abandoned is abandoned cleanly. The old workflow's memory goes
        with it, so coming back to it later starts a fresh request rather than
        resuming against times that were offered several minutes ago.
        """
        if extracted.intent is Intent.NEW_PATIENT and active != self.registration.name:
            # Not a model judgement, and the only intent exempt from needing
            # one. Every workflow that asks for a name and date of birth has
            # just invited this sentence in its retry prompt, and a caller who
            # takes the invitation while a booking sits half-finished must not
            # be answered with the same question again. Nothing is lost: they
            # have no record, so the workflow they are leaving had nothing to
            # act on.
            return self._abandon(session, active, wanted=self.registration.name)
        if not extracted.changes_subject:
            return False
        wanted = self._workflow_for(extracted.intent)
        if wanted is None or wanted == active:
            return False
        return self._abandon(session, active, wanted)

    def _abandon(self, session: SessionState, active: str, wanted: str) -> bool:
        """Put down the workflow in progress and clear what it was holding."""
        logger.info(
            "subject_changed", session_id=session.session_id, from_workflow=active, to=wanted
        )
        WorkflowMemory(session.workflow_state, active).clear()
        session.active_workflow = None
        return True

    def _workflow_for(self, intent: Intent) -> str | None:
        """The workflow an intent would start, if any."""
        match intent:
            case Intent.BOOK_APPOINTMENT:
                return self.booking.name
            case (
                Intent.LOOKUP_APPOINTMENT
                | Intent.CANCEL_APPOINTMENT
                | Intent.RESCHEDULE_APPOINTMENT
            ):
                return self.management.name
            case Intent.MEDICATION_LOOKUP:
                return self.lookup.name
            case Intent.COVERAGE_LOOKUP:
                return self.coverage.name
            case Intent.REFILL_REQUEST:
                return self.refill.name
            case Intent.NEW_PATIENT:
                return self.registration.name
            case Intent.CLINIC_FAQ:
                return self.faq.name
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
            if active == self.registration.name:
                return await self.registration.advance(
                    session, self._registration_input(extracted, utterance), now=now
                )
            if active == self.lookup.name:
                return await self.lookup.advance(
                    session, self._lookup_input(extracted, utterance), now=now
                )
            if active == self.coverage.name:
                return await self.coverage.advance(
                    session, self._coverage_input(extracted, utterance), now=now
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
    def _registration_input(self, extracted: ExtractedTurn, utterance: str) -> NewPatientInput:
        return NewPatientInput(
            utterance=utterance,
            identity=self._identity_input(extracted, utterance),
            phone=extracted.phone,
        )

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
            ordinal=extracted.ordinal,
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

    @staticmethod
    def _coverage_input(extracted: ExtractedTurn, utterance: str) -> CoverageLookupInput:
        return CoverageLookupInput(
            utterance=utterance,
            full_name=extracted.full_name,
            date_of_birth=extracted.date_of_birth,
            second_factor_value=extracted.second_factor_value,
        )

    # ------------------------------------------------------------- context
    def _context(self, session: SessionState) -> ExtractionContext:
        """What the last response left outstanding, so references resolve."""
        awaiting: AwaitedInput | None = None
        offers: tuple[SlotOffer, ...] = ()

        # Only the workflow that is actually running. Scanning both and taking
        # whichever had something left over let a stale list resolve a choice
        # it had nothing to do with: on a live call the times offered during a
        # booking eight turns earlier were still in memory, the caller said
        # "Tuesday" to pick between two *appointments*, and it matched the
        # second slot of the old booking list -- so the agent offered to cancel
        # the Wednesday appointment and called it Tuesday. A list nobody has
        # just read out cannot resolve anything.
        active = session.active_workflow
        if active is not None:
            raw = session.workflow_state.get(f"{active}.offers")
            if isinstance(raw, list) and raw:
                offers = tuple(SlotOffer.model_validate(item) for item in raw)

        last = session.workflow_state.get("_awaiting")
        if isinstance(last, str):
            awaiting = AwaitedInput(last)
        elif self._identity_pending(session):
            # The greeting asked its question before any turn existed to record
            # that it had. Derived here rather than written at session start,
            # so every way a call can open -- voice, the CLI, the HTTP API --
            # is waiting for the answer without each having to remember to.
            awaiting = (
                AwaitedInput.PATIENT_STATUS
                if session.workflow_state.get(PATIENT_STATUS) is None
                and not self.identity.has_started(session)
                else AwaitedInput.NAME
            )
        return ExtractionContext(awaiting=awaiting, offers=offers)

    # -------------------------------------------------------------- record
    async def _record(
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

        # And remember any yes-or-no the agent has just put to the caller. A
        # workflow that offers to book and then closes cannot hear "yes
        # please" itself; this is what lets the next turn act on it.
        if response is not None and response.offer is not None:
            session.workflow_state[PENDING_OFFER] = response.offer.value

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

        # A turn is the unit of work: everything it produced is written here,
        # in one transaction, rather than each service awaiting its own write.
        if self.persistence is not None:
            await self.persistence.flush_turn(session, trace)

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
        if extracted.dob_candidates:
            found["date_of_birth_readings"] = str(len(extracted.dob_candidates))
        if extracted.spelled:
            found["spelled"] = "<redacted>"
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

    async def _turn_limit_reached(
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
        # The call is being handed to a person, so it ends here. Left open, the
        # limit fires again on every further turn: one live call produced three
        # separate front-desk escalations for the same handover, which is three
        # tickets for one caller.
        session.end(moment)
        return await self._record(
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
