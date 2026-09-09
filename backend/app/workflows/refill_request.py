"""Requesting a refill (Phase 7).

SAFETY.md category B: an administrative workflow. The system may identify the
medication, locate the active prescription, create a request, and tell the
patient it was sent. It may not authorise the refill, create a prescription,
change a dose, or imply approval.

The wording at the end matters as much as the record: "sent for review" and
"approved" mean very different things to someone deciding whether they can
still take their medication tomorrow.
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
from app.services.base import ConflictError, UpstreamUnavailableError
from app.services.escalation_service import EscalationService
from app.services.medication_service import MedicationLookupStatus, MedicationService
from app.services.refill_service import RefillService
from app.services.verification_service import SecondFactorType, VerificationService
from app.workflows.base import AwaitedInput, WorkflowResponse, WorkflowStatus
from app.workflows.identity import IdentityCollector, IdentityOutcome
from app.workflows.medication_common import MedicationMemory

logger = get_logger(__name__)

WORKFLOW_NAME = "refill_request"


class RefillState(StrEnum):
    COLLECTING_IDENTITY = "COLLECTING_IDENTITY"
    AWAITING_SECOND_FACTOR = "AWAITING_SECOND_FACTOR"
    COLLECTING_MEDICATION = "COLLECTING_MEDICATION"
    CONFIRMING = "CONFIRMING"
    REQUESTED = "REQUESTED"
    ESCALATED = "ESCALATED"


class RefillRequestInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    utterance: str = ""
    full_name: str | None = None
    date_of_birth: date | None = None
    second_factor_type: SecondFactorType | None = None
    second_factor_value: str | None = None
    medication_name: str | None = None
    confirm: bool | None = None


class RefillRequestWorkflow:
    """Sends a refill request to the clinic for review."""

    name = WORKFLOW_NAME

    def __init__(
        self,
        verification: VerificationService,
        medications: MedicationService,
        refills: RefillService,
        escalations: EscalationService,
        audit: AuditService | None = None,
    ) -> None:
        self.medications = medications
        self.refills = refills
        self.escalations = escalations
        self.audit = audit or AuditService()
        self.identity = IdentityCollector(verification, self.audit)

    async def start(self, session: SessionState) -> WorkflowResponse:
        memory = self._memory(session)
        session.active_workflow = self.name
        if memory.get("state") is None:
            memory.set(
                "state",
                RefillState.COLLECTING_MEDICATION.value
                if session.is_verified
                else RefillState.COLLECTING_IDENTITY.value,
            )
        state = RefillState(memory.get("state"))
        if state is RefillState.COLLECTING_IDENTITY:
            return self._respond(
                session,
                state,
                WorkflowStatus.AWAITING_INPUT,
                "I can send that request once I've confirmed who you are. "
                + IdentityCollector.ask().message,
                awaiting=AwaitedInput.IDENTITY,
            )
        return self._respond(
            session,
            RefillState.COLLECTING_MEDICATION,
            WorkflowStatus.AWAITING_INPUT,
            "Which medication would you like refilled?",
        )

    async def advance(
        self, session: SessionState, turn: RefillRequestInput, now: datetime | None = None
    ) -> WorkflowResponse:
        memory = self._memory(session)
        session.active_workflow = self.name
        state = RefillState(memory.get("state", RefillState.COLLECTING_IDENTITY.value))
        moment = now or datetime.now(UTC)

        try:
            if state is RefillState.COLLECTING_IDENTITY:
                result = await self.identity.submit_identity(
                    session, turn.full_name, turn.date_of_birth
                )
            elif state is RefillState.AWAITING_SECOND_FACTOR:
                result = await self.identity.submit_second_factor(
                    session, turn.second_factor_type, turn.second_factor_value
                )
            elif state is RefillState.CONFIRMING:
                return await self._handle_confirmation(session, turn, moment)
            else:
                return await self._identify_medication(session, turn, moment)

            if result.outcome is IdentityOutcome.VERIFIED:
                return await self._identify_medication(session, turn, moment)
            if result.outcome is IdentityOutcome.LOCKED_OUT:
                return self._locked_out(session, result.escalation_id)

            next_state = (
                RefillState.AWAITING_SECOND_FACTOR
                if result.outcome is IdentityOutcome.NEEDS_SECOND_FACTOR
                else RefillState.COLLECTING_IDENTITY
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
                f"Medication records unavailable: {exc}",
                "I can't reach your records at the moment. Let me pass you to our staff "
                "so they can take the request.",
            )

    # ------------------------------------------------------------ medication
    async def _identify_medication(
        self, session: SessionState, turn: RefillRequestInput, now: datetime
    ) -> WorkflowResponse:
        patient_ref = require_verified_patient(session)

        if not turn.medication_name:
            active = await self.medications.list_active(patient_ref)
            if not active:
                return self._escalate(
                    session,
                    turn,
                    "Patient asked for a refill but has no active prescriptions.",
                    "I don't see any active prescriptions on your record, so I can't send "
                    "a refill request. Let me pass you to our staff to check.",
                )
            names = ", ".join(m.display_name for m in active)
            return self._respond(
                session,
                RefillState.COLLECTING_MEDICATION,
                WorkflowStatus.AWAITING_INPUT,
                f"Which would you like refilled? Your record shows {names}.",
            )

        lookup = await self.medications.look_up(patient_ref, turn.medication_name)

        if lookup.status is MedicationLookupStatus.AMBIGUOUS:
            options = ", ".join(lookup.candidates)
            return self._respond(
                session,
                RefillState.COLLECTING_MEDICATION,
                WorkflowStatus.AWAITING_INPUT,
                f"I found more than one that matches — {options}. Which did you mean?",
            )

        if lookup.status is MedicationLookupStatus.NOT_FOUND:
            # No active prescription means there is nothing to refill. Do not
            # create a request against a prescription that does not exist.
            return self._escalate(
                session,
                turn,
                f"Refill requested for {turn.medication_name!r}, which is not an active "
                "prescription on this patient's record.",
                f"I can't find an active prescription for {turn.medication_name} on your "
                "record, so I can't send a refill request for it. Let me pass you to our "
                "staff to look into it.",
            )

        # FOUND or NO_DOSAGE_ON_FILE: the prescription is active either way, and
        # a missing instruction does not stop the clinician reviewing a refill.
        medication = lookup.medication
        assert medication is not None
        self._memory(session).set("medication", medication.model_dump(mode="json"))
        return self._respond(
            session,
            RefillState.CONFIRMING,
            WorkflowStatus.AWAITING_INPUT,
            f"I can send a refill request for {medication.display_name} to the clinic. "
            "Shall I do that?",
            awaiting=AwaitedInput.CONFIRMATION,
        )

    async def _handle_confirmation(
        self, session: SessionState, turn: RefillRequestInput, now: datetime
    ) -> WorkflowResponse:
        memory = self._memory(session)
        medication = MedicationSummary.model_validate(memory.get("medication"))

        if turn.confirm is None:
            return self._respond(
                session,
                RefillState.CONFIRMING,
                WorkflowStatus.AWAITING_INPUT,
                f"Would you like me to send a refill request for {medication.display_name}?",
                awaiting=AwaitedInput.CONFIRMATION,
            )

        if turn.confirm is False:
            session.active_workflow = None
            return self._respond(
                session,
                RefillState.REQUESTED,
                WorkflowStatus.COMPLETED,
                "No problem, I haven't sent anything. Is there anything else?",
            )

        patient_ref = require_verified_patient(session)
        try:
            request = self.refills.request_refill(
                patient_ref=patient_ref,
                medication=medication,
                session_id=session.session_id,
                now=now,
            )
        except ConflictError:
            session.active_workflow = None
            return self._respond(
                session,
                RefillState.REQUESTED,
                WorkflowStatus.COMPLETED,
                f"There's already a refill request for {medication.display_name} waiting "
                "to be reviewed, so I haven't sent another one.",
            )

        self.audit.record(
            AuditAction.REFILL_REQUESTED,
            session_id=session.session_id,
            patient_ref=patient_ref,
            resource_type="RefillRequest",
            resource_id=request.refill_request_id,
        )
        session.active_workflow = None
        # Says sent for review. Never approved -- the clinic decides, not us.
        return self._respond(
            session,
            RefillState.REQUESTED,
            WorkflowStatus.COMPLETED,
            f"I've sent a refill request for {medication.display_name} to the clinic for "
            "review. You'll hear back once a clinician has looked at it. Is there "
            "anything else?",
        )

    # ------------------------------------------------------------- helpers
    def _locked_out(self, session: SessionState, escalation_id: str | None) -> WorkflowResponse:
        session.active_workflow = None
        return self._respond(
            session,
            RefillState.ESCALATED,
            WorkflowStatus.ESCALATED,
            "I haven't been able to confirm your identity, so I'm passing you to our front desk.",
            escalation_id=escalation_id,
        )

    def _escalate(
        self, session: SessionState, turn: RefillRequestInput, summary: str, message: str
    ) -> WorkflowResponse:
        escalation = self.escalations.create(
            category=EscalationCategory.ADMINISTRATIVE,
            summary=summary,
            session_id=session.session_id,
            patient_ref=session.patient_ref,
            verification_state=session.verification,
            patient_question=turn.utterance or None,
            ai_action="No refill request created",
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
            RefillState.ESCALATED,
            WorkflowStatus.ESCALATED,
            message,
            escalation_id=escalation.escalation_id,
        )

    def _memory(self, session: SessionState) -> MedicationMemory:
        return MedicationMemory(session.workflow_state, self.name)

    def _respond(
        self,
        session: SessionState,
        state: RefillState,
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
