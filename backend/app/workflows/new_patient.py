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

*Only what is needed is asked for.* A name, a date of birth, a phone number,
and what they want to be seen about. No insurance identifiers, no social
security number, nothing a receptionist would take at the desk with a photo ID
in front of them.

What this does *not* do is treat registration as identity proofing. The record
is created from unverified assertions, which is exactly what a clinic does on
the phone, and the appointment says to bring photo ID -- the desk closes the
loop that a phone call cannot.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.agents.state import SessionState
from app.observability.logging import get_logger
from app.schemas.domain import AppointmentType, AuditAction, EscalationCategory
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
from app.workflows.existing_patient_booking import BookingInput, ExistingPatientBookingWorkflow

logger = get_logger(__name__)

WORKFLOW_NAME = "new_patient"


class RegistrationState(StrEnum):
    COLLECTING_NAME = "COLLECTING_NAME"
    COLLECTING_DOB = "COLLECTING_DOB"
    COLLECTING_PHONE = "COLLECTING_PHONE"
    COLLECTING_REASON = "COLLECTING_REASON"
    REGISTERED = "REGISTERED"
    ESCALATED = "ESCALATED"


FINISHED_STATES = frozenset({RegistrationState.REGISTERED.value})


class NewPatientInput(BaseModel):
    """What the orchestrator extracted from this turn."""

    model_config = ConfigDict(frozen=True)

    utterance: str = ""
    full_name: str | None = None
    date_of_birth: date | None = None
    #: The caller's own number, taken as spoken; the second factor on any
    #: future call is its last four digits.
    phone: str | None = None
    reason: str | None = None


class NewPatientWorkflow:
    """Takes a new patient's details, registers them, and books the first visit."""

    name = WORKFLOW_NAME

    def __init__(
        self,
        patients: PatientService,
        verification: VerificationService,
        booking: ExistingPatientBookingWorkflow,
        escalations: EscalationService,
        audit: AuditService | None = None,
    ) -> None:
        self.patients = patients
        self.verification = verification
        self.booking = booking
        self.escalations = escalations
        self.audit = audit or AuditService()

    async def start(self, session: SessionState) -> WorkflowResponse:
        memory = self._memory(session)
        session.active_workflow = self.name
        begin_request(
            memory, finished=FINISHED_STATES, fresh=RegistrationState.COLLECTING_NAME.value
        )
        return self._ask_for_what_is_missing(session, memory)

    async def advance(
        self, session: SessionState, turn: NewPatientInput, now: datetime | None = None
    ) -> WorkflowResponse:
        memory = self._memory(session)
        session.active_workflow = self.name
        begin_request(
            memory, finished=FINISHED_STATES, fresh=RegistrationState.COLLECTING_NAME.value
        )
        moment = now or datetime.now(UTC)

        # Callers volunteer everything at once -- "I'm new, I'm Nina Okafor,
        # born the third of May nineteen ninety" -- so each turn is absorbed
        # before the state is consulted, or the workflow asks for what it has
        # just been told.
        self._absorb(memory, turn)

        try:
            if memory.get("full_name") is None or memory.get("date_of_birth") is None:
                return self._ask_for_what_is_missing(session, memory)

            if not memory.get("checked_for_duplicate"):
                duplicate = await self._already_on_file(session, memory)
                if duplicate is not None:
                    return duplicate

            if memory.get("phone") is None:
                return self._respond(
                    session,
                    RegistrationState.COLLECTING_PHONE,
                    WorkflowStatus.AWAITING_INPUT,
                    "Thank you. And a contact phone number? We use the last four digits "
                    "to check it's you when you call back.",
                    awaiting=AwaitedInput.PHONE,
                )

            if memory.get("reason") is None:
                # Asked before the record is written, not after. The booking
                # workflow would normally ask this, but it is told the visit
                # type up front -- a first appointment is 45 minutes because
                # it is a first appointment -- so it has no reason to. Without
                # this the visit note would be whatever sentence the caller
                # opened the call with.
                return self._respond(
                    session,
                    RegistrationState.COLLECTING_REASON,
                    WorkflowStatus.AWAITING_INPUT,
                    "Thank you. And what would you like to be seen about?",
                    awaiting=AwaitedInput.REASON,
                )

            return await self._register_and_book(session, memory, turn, moment)
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
        if turn.full_name and len(turn.full_name.split()) >= 2:
            memory.set("full_name", " ".join(turn.full_name.split()))
        if turn.date_of_birth is not None:
            memory.set("date_of_birth", turn.date_of_birth.isoformat())
        if turn.phone:
            memory.set("phone", turn.phone)
        if turn.reason:
            memory.set("reason", turn.reason)

    def _ask_for_what_is_missing(
        self, session: SessionState, memory: WorkflowMemory
    ) -> WorkflowResponse:
        if memory.get("full_name") is None:
            return self._respond(
                session,
                RegistrationState.COLLECTING_NAME,
                WorkflowStatus.AWAITING_INPUT,
                "Happy to get you registered. Could I take your full name?",
                awaiting=AwaitedInput.IDENTITY,
            )
        return self._respond(
            session,
            RegistrationState.COLLECTING_DOB,
            WorkflowStatus.AWAITING_INPUT,
            "Thank you. And your date of birth?",
            awaiting=AwaitedInput.IDENTITY,
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
    async def _register_and_book(
        self,
        session: SessionState,
        memory: WorkflowMemory,
        turn: NewPatientInput,
        now: datetime,
    ) -> WorkflowResponse:
        given, _, family = str(memory.get("full_name")).partition(" ")
        try:
            patient = await self.patients.register_new_patient(
                given_name=given,
                family_name=family,
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

        # Booking is not reimplemented here. The first visit is longer, and
        # that is the only difference -- so the booking workflow runs it, with
        # the type it cannot infer from a reason handed to it.
        await self.booking.start(session, appointment_type=AppointmentType.NEW_PATIENT)
        booked = await self.booking.advance(
            session,
            BookingInput(utterance=turn.utterance, reason=str(memory.get("reason") or "") or None),
            now=now,
        )
        return booked.model_copy(update={"message": f"Thanks, you're registered. {booked.message}"})

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
        return self._respond(
            session,
            RegistrationState.COLLECTING_DOB,
            WorkflowStatus.AWAITING_INPUT,
            "That date of birth doesn't look right to me. Could you say it again?",
            awaiting=AwaitedInput.IDENTITY,
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
