"""Telling a verified patient what cover the clinic has on file.

SAFETY.md category A, like the medication read: information a human already
recorded, returned as recorded. The plan name and member number come straight
off the record.

What it will not do is talk about money. "What will this cost me" depends on a
live deductible and a negotiated rate the clinic does not hold, and a confident
figure that turns out to be wrong is worse than no figure -- so cost questions
go to the front desk, answered from clinic_faq without ever touching a record.

Three outcomes, and the difference between them matters to the caller:

* **active** -- read the plan back.
* **lapsed** -- on file, but cancelled or run out. Saying "nothing on file"
  here would be both wrong and alarming to someone holding a card.
* **none** -- nothing recorded. An ordinary case, not an error: plenty of
  patients have never handed over a card.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.agents.state import SessionState
from app.observability.logging import get_logger
from app.schemas.domain import AuditAction, EscalationCategory
from app.services.access_control import require_verified_patient
from app.services.audit_service import AuditService
from app.services.base import UpstreamUnavailableError
from app.services.coverage_service import CoverageService, CoverageStatus
from app.services.escalation_service import EscalationService
from app.services.verification_service import SecondFactorType, VerificationService
from app.workflows.base import AwaitedInput, WorkflowResponse, WorkflowStatus
from app.workflows.identity import IdentityCollector, IdentityOutcome

logger = get_logger(__name__)

WORKFLOW_NAME = "coverage_lookup"


class CoverageState(StrEnum):
    COLLECTING_IDENTITY = "COLLECTING_IDENTITY"
    AWAITING_SECOND_FACTOR = "AWAITING_SECOND_FACTOR"
    ANSWERED = "ANSWERED"
    ESCALATED = "ESCALATED"


class CoverageLookupInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    utterance: str = ""
    full_name: str | None = None
    date_of_birth: date | None = None
    second_factor_type: SecondFactorType | None = None
    second_factor_value: str | None = None


class CoverageLookupWorkflow:
    """Reads back the insurance cover recorded for a verified patient."""

    name = WORKFLOW_NAME

    def __init__(
        self,
        verification: VerificationService,
        coverage: CoverageService,
        escalations: EscalationService,
        audit: AuditService | None = None,
    ) -> None:
        self.coverage = coverage
        self.escalations = escalations
        self.audit = audit or AuditService()
        self.identity = IdentityCollector(verification, self.audit)

    async def start(self, session: SessionState) -> WorkflowResponse:
        memory = self._memory(session)
        session.active_workflow = self.name
        if memory.get("state") is None:
            memory.set(
                "state",
                CoverageState.ANSWERED.value
                if session.is_verified
                else CoverageState.COLLECTING_IDENTITY.value,
            )
        if CoverageState(memory.get("state")) is CoverageState.COLLECTING_IDENTITY:
            return self._respond(
                session,
                CoverageState.COLLECTING_IDENTITY,
                WorkflowStatus.AWAITING_INPUT,
                "I can check what we have on file once I've confirmed who you are. "
                + IdentityCollector.ask().message,
                awaiting=AwaitedInput.IDENTITY,
            )
        return self._respond(
            session,
            CoverageState.COLLECTING_IDENTITY,
            WorkflowStatus.AWAITING_INPUT,
            IdentityCollector.ask().message,
            awaiting=AwaitedInput.IDENTITY,
        )

    async def advance(
        self, session: SessionState, turn: CoverageLookupInput, now: datetime | None = None
    ) -> WorkflowResponse:
        memory = self._memory(session)
        session.active_workflow = self.name
        state = CoverageState(memory.get("state", CoverageState.COLLECTING_IDENTITY.value))
        moment = now or datetime.now(UTC)

        try:
            if state is CoverageState.COLLECTING_IDENTITY:
                result = await self.identity.submit_identity(
                    session, turn.full_name, turn.date_of_birth
                )
            elif state is CoverageState.AWAITING_SECOND_FACTOR:
                result = await self.identity.submit_second_factor(
                    session, turn.second_factor_type, turn.second_factor_value
                )
            else:
                return await self._answer(session, moment)

            if result.outcome is IdentityOutcome.VERIFIED:
                return await self._answer(session, moment)
            if result.outcome is IdentityOutcome.LOCKED_OUT:
                return self._locked_out(session, result.escalation_id)

            next_state = (
                CoverageState.AWAITING_SECOND_FACTOR
                if result.outcome is IdentityOutcome.NEEDS_SECOND_FACTOR
                else CoverageState.COLLECTING_IDENTITY
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
                f"Coverage records unavailable: {exc}",
                "I can't reach your records at the moment. Let me pass you to our staff "
                "so they can check for you.",
            )

    # -------------------------------------------------------------- answer
    async def _answer(self, session: SessionState, now: datetime) -> WorkflowResponse:
        patient_ref = require_verified_patient(session)
        result = await self.coverage.for_patient(patient_ref, today=now.date())

        self.audit.record(
            AuditAction.COVERAGE_READ,
            session_id=session.session_id,
            patient_ref=patient_ref,
            resource_type="Coverage",
            detail=result.status.value,
        )

        if result.status is CoverageStatus.NONE:
            return self._answered(
                session,
                "I don't have any insurance on file for you. Our front desk can add it "
                "if you bring your card, or take the details over the phone.",
            )

        assert result.coverage is not None  # NONE is the only case without one
        plan = result.coverage.plan_name

        if result.status is CoverageStatus.LAPSED:
            ended = result.coverage.period_end
            when = f" It ran out on {ended:%d %B %Y}." if ended else ""
            return self._answered(
                session,
                f"The cover we have on file for you is {plan}, and our record shows it is "
                f"no longer active.{when} Our front desk can update it if that's changed.",
            )

        others = [c.plan_name for c in result.all_coverage[1:]]
        also = f" We also have {', '.join(others)} on file." if others else ""
        return self._answered(
            session,
            f"Our record shows you're covered by {plan}.{also} I can't tell you what a "
            "visit will cost, though — that depends on your plan and deductible, and "
            "our front desk can check it for you.",
        )

    # ------------------------------------------------------------ outcomes
    def _answered(self, session: SessionState, message: str) -> WorkflowResponse:
        session.active_workflow = None
        return self._respond(session, CoverageState.ANSWERED, WorkflowStatus.COMPLETED, message)

    def _locked_out(self, session: SessionState, escalation_id: str | None) -> WorkflowResponse:
        session.active_workflow = None
        return self._respond(
            session,
            CoverageState.ESCALATED,
            WorkflowStatus.ESCALATED,
            "I haven't been able to confirm your identity, so I'm passing you to our front desk.",
            escalation_id=escalation_id,
        )

    def _escalate(
        self,
        session: SessionState,
        turn: CoverageLookupInput,
        summary: str,
        message: str,
    ) -> WorkflowResponse:
        escalation = self.escalations.create(
            category=EscalationCategory.SYSTEM_UNCERTAINTY,
            summary=summary,
            session_id=session.session_id,
            patient_ref=session.patient_ref,
            verification_state=session.verification,
            patient_question=turn.utterance or None,
            ai_action="No coverage information given",
        )
        session.active_workflow = None
        return self._respond(
            session,
            CoverageState.ESCALATED,
            WorkflowStatus.ESCALATED,
            message,
            escalation_id=escalation.escalation_id,
        )

    # ------------------------------------------------------------- plumbing
    def _respond(
        self,
        session: SessionState,
        state: CoverageState,
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

    def _memory(self, session: SessionState):  # type: ignore[no-untyped-def]
        from app.workflows.base import WorkflowMemory

        return WorkflowMemory(session.workflow_state, self.name)
