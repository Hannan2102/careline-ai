"""Registering somebody the clinic has never seen.

Every other workflow starts by proving the caller is already in the record.
This one starts from the opposite fact, and that inverts the safety argument
rather than removing it.

**Why the agent is allowed to create a record at all.** Everywhere else, the
gate exists because the record contains things the caller did not tell us --
their prescriptions, their appointments, their cover -- and handing those to
the wrong person is the harm. A record created during this call contains
nothing but what the caller has just said out loud. There is no history to
disclose, so verifying them against it would be verifying them against their
own sentence. The clinic's published process says the same thing in plainer
words: "we'll take your details over the phone" (config/clinic.py).

Three things guard it.

*A caller has to say they are new.* Failing verification never routes here.
Somebody who has misremembered their date of birth is not a new patient, and
an agent that offered registration after a failed attempt would manufacture
duplicate records out of ordinary human error -- which is a clinical hazard,
not an inconvenience: it is how a prescription ends up on a record nobody
reads. The identity prompt mentions that registration exists, and the caller
must take it.

*A possible duplicate stops the flow.* Name and date of birth are checked
before anything is written, and a match hands over to the front desk rather
than creating a second record for a person who already has one.

*Only what is needed is asked for.* A name, a date of birth and a phone
number. What they want to be seen about is asked when they book, if they
book. No insurance identifiers, no social
security number, nothing a receptionist would take at the desk with a photo ID
in front of them.

*The name and date of birth are confirmed, not just heard.* They are taken by
the same steps every caller goes through (identity.py): the name spelled back,
the date read back. A caller who has already confirmed them this call -- at
the opening, or in an attempt that then failed to match -- is not asked again.

What this does *not* do is treat registration as identity proofing. The record
is created from unverified assertions, which is exactly what a clinic does on
the phone, and the appointment says to bring photo ID -- the desk closes the
loop that a phone call cannot.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.agents.spelling import NameParts, split_name
from app.agents.state import SessionState
from app.observability.logging import get_logger
from app.schemas.domain import AuditAction, EscalationCategory
from app.services.audit_service import AuditService
from app.services.base import UpstreamUnavailableError, ValidationError
from app.services.escalation_service import EscalationService
from app.services.patient_service import PatientService
from app.services.verification_service import VerificationService
from app.workflows.base import (
    AwaitedInput,
    WorkflowMemory,
    WorkflowResponse,
    WorkflowStatus,
    begin_request,
)
from app.workflows.identity import (
    ASK_NAME,
    IdentityCollector,
    IdentityInput,
    IdentityOutcome,
    IdentityResult,
    IdentityStep,
)

logger = get_logger(__name__)

WORKFLOW_NAME = "new_patient"

OPENING = f"Happy to get you registered. {ASK_NAME}"

#: Said once registered: what a new patient can do now. Not the general menu,
#: which leads with changing appointments and prescriptions a minute-old
#: record cannot have.
WHAT_I_CAN_DO = (
    "I can book your first appointment, answer questions about the clinic like our "
    "hours, location or the insurance we accept, and once you've been seen, help with "
    "prescriptions and refills. What would you like to do?"
)

#: Set when a patient is registered this call, until their first booking.
FIRST_VISIT = "_new_patient.first_visit"
_REGISTERED_GIVEN = "_new_patient.given_name"


class RegistrationState(StrEnum):
    COLLECTING_NAME = "COLLECTING_NAME"
    COLLECTING_DOB = "COLLECTING_DOB"
    COLLECTING_PHONE = "COLLECTING_PHONE"
    REGISTERED = "REGISTERED"
    ESCALATED = "ESCALATED"


FINISHED_STATES = frozenset({RegistrationState.REGISTERED.value})


class NewPatientInput(BaseModel):
    """What the orchestrator extracted from this turn."""

    model_config = ConfigDict(frozen=True)

    utterance: str = ""
    #: Whatever this turn said towards a name and date of birth, for the
    #: shared identity steps to read.
    identity: IdentityInput = IdentityInput()
    #: The caller's own number, taken as spoken; the second factor on any
    #: future call is its last four digits.
    phone: str | None = None


class NewPatientWorkflow:
    """Takes a new patient's details and registers them; their first booking is the long one."""

    name = WORKFLOW_NAME

    def __init__(
        self,
        patients: PatientService,
        verification: VerificationService,
        escalations: EscalationService,
        audit: AuditService | None = None,
    ) -> None:
        self.patients = patients
        self.verification = verification
        self.escalations = escalations
        self.audit = audit or AuditService()
        self.identity = IdentityCollector(verification, self.audit)

    async def start(self, session: SessionState) -> WorkflowResponse:
        memory = self._memory(session)
        session.active_workflow = self.name
        begin_request(
            memory, finished=FINISHED_STATES, fresh=RegistrationState.COLLECTING_NAME.value
        )
        if self.identity.confirmed_identity(session) is not None:
            return self._ask_for_phone(session)
        return self._respond(
            session,
            RegistrationState.COLLECTING_NAME,
            WorkflowStatus.AWAITING_INPUT,
            OPENING,
            awaiting=AwaitedInput.NAME,
        )

    async def advance(
        self, session: SessionState, turn: NewPatientInput, now: datetime | None = None
    ) -> WorkflowResponse:
        memory = self._memory(session)
        session.active_workflow = self.name
        begin_request(
            memory, finished=FINISHED_STATES, fresh=RegistrationState.COLLECTING_NAME.value
        )
        # Callers volunteer everything at once -- "I'm new, I'm Nina Okafor,
        # born the third of May nineteen ninety" -- so each turn is absorbed
        # before the state is consulted, or the workflow asks for what it has
        # just been told.
        self._absorb(memory, turn)

        try:
            if memory.get("full_name") is None or memory.get("date_of_birth") is None:
                waiting = self._collect_identity(session, memory, turn)
                if waiting is not None:
                    return waiting

            if not memory.get("checked_for_duplicate"):
                duplicate = await self._already_on_file(session, memory)
                if duplicate is not None:
                    return duplicate

            if memory.get("phone") is None:
                return self._ask_for_phone(session)

            return await self._register(session, memory)
        except UpstreamUnavailableError as exc:
            return self._escalate(
                session,
                turn,
                f"Patient records unavailable during registration: {exc}",
                "I can't reach our records at the moment. Let me pass you to our front "
                "desk, who can register you.",
            )

    # ----------------------------------------------------------- collecting
    @staticmethod
    def _absorb(memory: WorkflowMemory, turn: NewPatientInput) -> None:
        if turn.phone:
            memory.set("phone", turn.phone)

    def _collect_identity(
        self, session: SessionState, memory: WorkflowMemory, turn: NewPatientInput
    ) -> WorkflowResponse | None:
        """A confirmed name and date of birth, or the next question towards one.

        Reused when the caller has already confirmed them this call -- at the
        opening, or in an attempt that failed to match before they said they
        were new. Otherwise taken by the same steps every caller goes through,
        not a copy of them: a second implementation of "spell the name back"
        is a second place for the guarantees in identity.py to drift.

        ``None`` once both are in memory.
        """
        confirmed = self.identity.confirmed_identity(session)
        if confirmed is None:
            first_turn = not memory.get("collecting")
            memory.set("collecting", True)
            result = (
                # The sentence that started the registration is not an answer
                # to whatever identity question was outstanding when it was said.
                self.identity.prompt(session)
                if first_turn and turn.identity.is_empty
                else self.identity.collect(session, turn.identity)
            )
            if result.outcome is IdentityOutcome.HANDED_OVER:
                session.active_workflow = None
                return self._respond(
                    session,
                    RegistrationState.ESCALATED,
                    WorkflowStatus.ESCALATED,
                    result.message,
                    escalation_id=result.escalation_id,
                )
            if result.outcome is not IdentityOutcome.COLLECTED:
                return self._identity_question(session, memory, result.message, result)
            confirmed = self.identity.confirmed_identity(session)
            assert confirmed is not None  # COLLECTED means both halves are confirmed

        name, born = confirmed
        # Two words at least, as before: a record needs a family name, and a
        # given name alone matches half the clinic in the duplicate check.
        if name.family is None or len(name.full.split()) < 2:
            self.identity.forget(session)
            return self._identity_question(session, memory, OPENING, None)
        memory.set("name", name.as_dict())
        memory.set("full_name", name.full)
        memory.set("date_of_birth", born.isoformat())
        return None

    def _identity_question(
        self,
        session: SessionState,
        memory: WorkflowMemory,
        message: str,
        result: IdentityResult | None,
    ) -> WorkflowResponse:
        step = (result.step if result else None) or IdentityStep.ASKING_NAME
        awaiting = (result.awaiting if result else None) or AwaitedInput.NAME
        asking_for_name = step in (
            IdentityStep.ASKING_NAME,
            IdentityStep.CONFIRMING_NAME,
            IdentityStep.SPELLING_NAME,
        )
        # The first time the name is asked for inside a registration, say why
        # -- unless only half of it is missing ("Thanks, Nina. And your last
        # name?"), which already says enough.
        if (
            step is IdentityStep.ASKING_NAME
            and not memory.get("asked_for_name")
            and not message.startswith("Thanks,")
        ):
            message = OPENING
        if asking_for_name:
            memory.set("asked_for_name", True)
        return self._respond(
            session,
            RegistrationState.COLLECTING_NAME
            if asking_for_name
            else RegistrationState.COLLECTING_DOB,
            WorkflowStatus.AWAITING_INPUT,
            message,
            awaiting=awaiting,
        )

    def _ask_for_phone(self, session: SessionState) -> WorkflowResponse:
        return self._respond(
            session,
            RegistrationState.COLLECTING_PHONE,
            WorkflowStatus.AWAITING_INPUT,
            "Thank you. And a contact phone number? We use the last four digits "
            "to check it's you when you call back.",
            awaiting=AwaitedInput.PHONE,
        )

    # ------------------------------------------------------------ duplicate
    async def _already_on_file(
        self, session: SessionState, memory: WorkflowMemory
    ) -> WorkflowResponse | None:
        """Stop before writing if these details already belong to somebody.

        A second record for a person who has one is the failure that matters
        here: their history sits under the old reference, and the clinician
        reading the new one sees a patient with no allergies and no
        medications. Front desk, not a merge attempted over the phone.

        This does tell a caller whether a name and date of birth are already
        known, which is a disclosure -- documented, and accepted, in SAFETY.md.
        Closing it would mean no new patient could ever register themselves.
        """
        full_name = str(memory.get("full_name"))
        born = date.fromisoformat(str(memory.get("date_of_birth")))
        memory.set("checked_for_duplicate", True)

        candidates = await self.patients.find_candidates(full_name, born)
        self.audit.record(
            AuditAction.PATIENT_SEARCHED,
            session_id=session.session_id,
            resource_type="Patient",
            detail=f"{len(candidates)} matching, pre-registration",
        )
        if not candidates:
            return None

        logger.info(
            "registration_stopped_possible_duplicate",
            session_id=session.session_id,
            candidates=len(candidates),
        )
        return self._escalate_response(
            session,
            EscalationCategory.ADMINISTRATIVE,
            "Registration requested for details already on file; possible duplicate.",
            "It looks as though we may already have you on file, and I don't want to "
            "create a second record for you. Let me pass you to our front desk, who can "
            "check and get you booked in.",
        )

    # ------------------------------------------------------------ the write
    async def _register(self, session: SessionState, memory: WorkflowMemory) -> WorkflowResponse:
        name = NameParts.from_dict(memory.get("name")) or split_name(str(memory.get("full_name")))
        given = " ".join(part for part in (name.given, name.middle) if part)
        try:
            patient = await self.patients.register_new_patient(
                given_name=given,
                family_name=name.family or "",
                date_of_birth=date.fromisoformat(str(memory.get("date_of_birth"))),
                phone=str(memory.get("phone")),
            )
        except ValidationError as exc:
            # Something the caller said will not do -- a date in the future, a
            # phone number three digits long. Ask again for that one thing
            # rather than starting over or handing them off.
            return self._ask_again(session, memory, exc)

        self.audit.record(
            AuditAction.PATIENT_REGISTERED,
            session_id=session.session_id,
            patient_ref=patient.reference,
            resource_type="Patient",
            resource_id=patient.reference,
        )
        # The record holds only what this caller has just said, so letting them
        # act on it discloses nothing they did not supply. The decision still
        # comes from the verification service, because that is the one place
        # allowed to open the gate (ADR 003).
        self.verification.accept_registration(session, patient)
        memory.set("state", RegistrationState.REGISTERED.value)
        # The details are on the record now; holding a second copy in the
        # identity steps would only be a way for them to resurface.
        self.identity.forget(session)
        # Whatever they book next is a first visit, and a first visit is the
        # long one. Kept for the orchestrator, which starts the booking.
        session.workflow_state[FIRST_VISIT] = True
        session.workflow_state[_REGISTERED_GIVEN] = name.given
        session.active_workflow = None

        # Registration ends here, with what the agent can now do for them.
        # It used to go straight on to booking, which assumed that everyone
        # who registers wants an appointment this minute -- and asked a caller
        # who had just spent six turns giving their details what their visit
        # was "about" before they had said they wanted one.
        return self._respond(
            session,
            RegistrationState.REGISTERED,
            WorkflowStatus.COMPLETED,
            f"{self.registered_line(session)} {WHAT_I_CAN_DO}",
        )

    # --------------------------------------------------- for the orchestrator
    @staticmethod
    def registered_line(session: SessionState) -> str:
        given = session.workflow_state.get(_REGISTERED_GIVEN)
        return f"Thanks, {given}, you're registered." if given else "Thanks, you're registered."

    def just_registered(self, response: WorkflowResponse) -> bool:
        return response.workflow == self.name and response.state == RegistrationState.REGISTERED

    @staticmethod
    def first_visit_pending(session: SessionState) -> bool:
        """Whether the booking about to start is this new patient's first.

        Asked once: the first booking after registering is the 45-minute new
        patient visit, and a second booking in the same call is an ordinary one.
        """
        return bool(session.workflow_state.pop(FIRST_VISIT, False))

    def _ask_again(
        self, session: SessionState, memory: WorkflowMemory, exc: ValidationError
    ) -> WorkflowResponse:
        detail = str(exc).lower()
        if "phone" in detail:
            memory.set("phone", None)
            return self._respond(
                session,
                RegistrationState.COLLECTING_PHONE,
                WorkflowStatus.AWAITING_INPUT,
                "Sorry, I didn't catch a full phone number. Could you say it again?",
                awaiting=AwaitedInput.PHONE,
            )
        memory.set("date_of_birth", None)
        memory.set("checked_for_duplicate", False)
        self.identity.reject_date_of_birth(session)
        return self._respond(
            session,
            RegistrationState.COLLECTING_DOB,
            WorkflowStatus.AWAITING_INPUT,
            "That date of birth doesn't look right to me. Could you say it again?",
            awaiting=AwaitedInput.DATE_OF_BIRTH,
        )

    # ------------------------------------------------------------- helpers
    def _escalate(
        self, session: SessionState, turn: NewPatientInput, summary: str, message: str
    ) -> WorkflowResponse:
        return self._escalate_response(
            session,
            EscalationCategory.SYSTEM_UNCERTAINTY,
            summary,
            message,
            question=turn.utterance or None,
        )

    def _escalate_response(
        self,
        session: SessionState,
        category: EscalationCategory,
        summary: str,
        message: str,
        question: str | None = None,
    ) -> WorkflowResponse:
        escalation = self.escalations.create(
            category=category,
            summary=summary,
            session_id=session.session_id,
            patient_ref=session.patient_ref,
            verification_state=session.verification,
            patient_question=question,
            ai_action="No record created",
        )
        self.audit.record(
            AuditAction.ESCALATION_CREATED,
            session_id=session.session_id,
            resource_type="Escalation",
            resource_id=escalation.escalation_id,
        )
        session.active_workflow = None
        self.identity.forget(session)
        return self._respond(
            session,
            RegistrationState.ESCALATED,
            WorkflowStatus.ESCALATED,
            message,
            escalation_id=escalation.escalation_id,
        )

    def _memory(self, session: SessionState) -> WorkflowMemory:
        return WorkflowMemory(session.workflow_state, self.name)

    def _respond(
        self,
        session: SessionState,
        state: RegistrationState,
        status: WorkflowStatus,
        message: str,
        awaiting: AwaitedInput | None = None,
        escalation_id: str | None = None,
    ) -> WorkflowResponse:
        self._memory(session).set("state", state.value)
        return WorkflowResponse(
            workflow=self.name,
            state=state.value,
            status=status,
            message=message,
            awaiting=awaiting,
            escalation_id=escalation_id,
        )
