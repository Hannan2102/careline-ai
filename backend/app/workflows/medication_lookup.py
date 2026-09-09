"""Reading back a prescribed medication (Phase 6).

SAFETY.md category A: retrieval of information a clinician already recorded.
The dosage is returned **exactly as stored** -- passed through as a quoted
value, attributed to the record, and checked before it is spoken.

Three outcomes are not answers, and each is handled differently: the patient
has no such prescription, the name matched several, or the record carries no
instruction text. That last one escalates. Assembling a dosage from the
structured fields would be a clinical act.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.agents.state import SessionState
from app.observability.logging import get_logger
from app.schemas.domain import AuditAction, EscalationCategory, MedicationSummary
from app.services.access_control import require_verified_patient
from app.services.audit_service import AuditService
from app.services.base import UpstreamUnavailableError
from app.services.escalation_service import EscalationService
from app.services.medication_service import MedicationLookupStatus, MedicationService
from app.services.verification_service import SecondFactorType, VerificationService
from app.workflows.base import AwaitedInput, WorkflowResponse, WorkflowStatus
from app.workflows.identity import IdentityCollector, IdentityOutcome
from app.workflows.medication_common import MedicationMemory

logger = get_logger(__name__)

WORKFLOW_NAME = "medication_lookup"


class LookupState(StrEnum):
    COLLECTING_IDENTITY = "COLLECTING_IDENTITY"
    AWAITING_SECOND_FACTOR = "AWAITING_SECOND_FACTOR"
    COLLECTING_MEDICATION = "COLLECTING_MEDICATION"
    ANSWERED = "ANSWERED"
    ESCALATED = "ESCALATED"


class MedicationLookupInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    utterance: str = ""
    full_name: str | None = None
    date_of_birth: date | None = None
    second_factor_type: SecondFactorType | None = None
    second_factor_value: str | None = None
    #: The medication the patient named, as they said it.
    medication_name: str | None = None
    #: True when they asked what they are taking rather than about one drug.
    list_all: bool = False


def render_dosage_answer(medication: MedicationSummary, service: MedicationService) -> str:
    """Build the spoken answer and verify the instruction survived intact.

    The template already guarantees this. The check exists because Phase 9 lets
    a model word the sentence around the dosage, and this is the assertion that
    it did not word the dosage itself.
    """
    answer = service.describe_dosage(medication)
    if not service.dosage_is_verbatim(answer, medication):  # pragma: no cover - guard
        raise RuntimeError(
            "rendered dosage does not match the stored instruction; refusing to speak it"
        )
    return answer


class MedicationLookupWorkflow:
    """Tells a verified patient what their prescription says."""

    name = WORKFLOW_NAME

    def __init__(
        self,
        verification: VerificationService,
        medications: MedicationService,
        escalations: EscalationService,
        audit: AuditService | None = None,
    ) -> None:
        self.medications = medications
        self.escalations = escalations
        self.audit = audit or AuditService()
        self.identity = IdentityCollector(verification, self.audit)

    async def start(self, session: SessionState) -> WorkflowResponse:
        memory = self._memory(session)
        session.active_workflow = self.name
        if memory.get("state") is None:
            memory.set(
                "state",
                LookupState.COLLECTING_MEDICATION.value
                if session.is_verified
                else LookupState.COLLECTING_IDENTITY.value,
            )
        state = LookupState(memory.get("state"))
        if state is LookupState.COLLECTING_IDENTITY:
            return self._respond(
                session,
                state,
                WorkflowStatus.AWAITING_INPUT,
                "I can look that up once I've confirmed who you are. "
                + IdentityCollector.ask().message,
                awaiting=AwaitedInput.IDENTITY,
            )
        return self._ask_which_medication(session)

    async def advance(
        self, session: SessionState, turn: MedicationLookupInput, now: datetime | None = None
    ) -> WorkflowResponse:
        memory = self._memory(session)
        session.active_workflow = self.name
        state = LookupState(memory.get("state", LookupState.COLLECTING_IDENTITY.value))
        moment = now or datetime.now(UTC)

        try:
            if state is LookupState.COLLECTING_IDENTITY:
                result = await self.identity.submit_identity(
                    session, turn.full_name, turn.date_of_birth
                )
            elif state is LookupState.AWAITING_SECOND_FACTOR:
                result = await self.identity.submit_second_factor(
                    session, turn.second_factor_type, turn.second_factor_value
                )
            else:
                return await self._answer(session, turn, moment)

            if result.outcome is IdentityOutcome.VERIFIED:
                return await self._answer(session, turn, moment)
            if result.outcome is IdentityOutcome.LOCKED_OUT:
                return self._locked_out(session, result.escalation_id)

            next_state = (
                LookupState.AWAITING_SECOND_FACTOR
                if result.outcome is IdentityOutcome.NEEDS_SECOND_FACTOR
                else LookupState.COLLECTING_IDENTITY
            )
            return self._respond(
                session,
                next_state,
                WorkflowStatus.AWAITING_INPUT,
                result.message,
                awaiting=result.awaiting,
            )
        except UpstreamUnavailableError as exc:
            return self._escalate(
                session,
                turn,
                EscalationCategory.SYSTEM_UNCERTAINTY,
                f"Medication records unavailable: {exc}",
                "I can't reach your records at the moment. Let me pass you to our staff "
                "so they can check for you.",
            )

    # -------------------------------------------------------------- answer
    async def _answer(
        self, session: SessionState, turn: MedicationLookupInput, now: datetime
    ) -> WorkflowResponse:
        patient_ref = require_verified_patient(session)

        if turn.list_all or not turn.medication_name:
            active = await self.medications.list_active(patient_ref)
            self.audit.record(
                AuditAction.MEDICATIONS_READ,
                session_id=session.session_id,
                patient_ref=patient_ref,
                resource_type="MedicationRequest",
                detail=f"{len(active)} active",
            )
            if not active:
                return self._answered(
                    session,
                    "I don't see any active prescriptions on your record. If that seems "
                    "wrong, I can pass you to our staff to check.",
                )
            names = ", ".join(m.display_name for m in active)
            if turn.list_all:
                return self._answered(
                    session,
                    f"Your record shows {names}. Would you like the instructions for any of those?",
                )
            return self._respond(
                session,
                LookupState.COLLECTING_MEDICATION,
                WorkflowStatus.AWAITING_INPUT,
                f"Which medication did you mean? Your record shows {names}.",
            )

        lookup = await self.medications.look_up(patient_ref, turn.medication_name)
        self.audit.record(
            AuditAction.MEDICATIONS_READ,
            session_id=session.session_id,
            patient_ref=patient_ref,
            resource_type="MedicationRequest",
            resource_id=(lookup.medication.medication_request_id if lookup.medication else None),
            detail=lookup.status.value,
        )

        match lookup.status:
            case MedicationLookupStatus.FOUND:
                assert lookup.medication is not None
                answer = render_dosage_answer(lookup.medication, self.medications)
                return self._answered(session, answer)

            case MedicationLookupStatus.AMBIGUOUS:
                options = ", ".join(lookup.candidates)
                return self._respond(
                    session,
                    LookupState.COLLECTING_MEDICATION,
                    WorkflowStatus.AWAITING_INPUT,
                    f"I found more than one that matches — {options}. Which did you mean?",
                )

            case MedicationLookupStatus.NOT_FOUND:
                active = await self.medications.list_active(patient_ref)
                names = ", ".join(m.display_name for m in active)
                suffix = f" Your record shows {names}." if names else ""
                return self._answered(
                    session,
                    f"I can't find an active prescription for {turn.medication_name} on "
                    f"your record.{suffix} I can pass you to our staff if that doesn't "
                    "look right.",
                )

            case MedicationLookupStatus.NO_DOSAGE_ON_FILE:
                assert lookup.medication is not None
                return self._escalate(
                    session,
                    turn,
                    EscalationCategory.CLINICAL,
                    f"Active prescription {lookup.medication.medication_request_id} has no "
                    "dosage instruction recorded; patient asked what to take.",
                    "I can see that prescription on your record, but the instructions "
                    "aren't recorded, and I don't want to guess. I've passed this to our "
                    "clinical staff and someone will come back to you.",
                    medication_display=lookup.medication.display_name,
                )

    # ------------------------------------------------------------- helpers
    def _ask_which_medication(self, session: SessionState) -> WorkflowResponse:
        return self._respond(
            session,
            LookupState.COLLECTING_MEDICATION,
            WorkflowStatus.AWAITING_INPUT,
            "Which medication would you like me to check?",
        )

    def _answered(self, session: SessionState, message: str) -> WorkflowResponse:
        session.active_workflow = None
        return self._respond(session, LookupState.ANSWERED, WorkflowStatus.COMPLETED, message)

    def _locked_out(self, session: SessionState, escalation_id: str | None) -> WorkflowResponse:
        session.active_workflow = None
        return self._respond(
            session,
            LookupState.ESCALATED,
            WorkflowStatus.ESCALATED,
            "I haven't been able to confirm your identity, so I'm passing you to our front desk.",
            escalation_id=escalation_id,
        )

    def _escalate(
        self,
        session: SessionState,
        turn: MedicationLookupInput,
        category: EscalationCategory,
        summary: str,
        message: str,
        medication_display: str | None = None,
    ) -> WorkflowResponse:
        escalation = self.escalations.create(
            category=category,
            summary=summary,
            session_id=session.session_id,
            patient_ref=session.patient_ref,
            verification_state=session.verification,
            patient_question=turn.utterance or None,
            medication_display=medication_display,
            ai_action="No dosage stated; record incomplete",
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
            LookupState.ESCALATED,
            WorkflowStatus.ESCALATED,
            message,
            escalation_id=escalation.escalation_id,
        )

    def _memory(self, session: SessionState) -> MedicationMemory:
        return MedicationMemory(session.workflow_state, self.name)

    def _respond(
        self,
        session: SessionState,
        state: LookupState,
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
