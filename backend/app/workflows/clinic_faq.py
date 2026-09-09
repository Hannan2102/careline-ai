"""Clinic information.

Answered from the structured clinic table, not from a model and not from a
vector store. Opening hours are a lookup: retrieval would be slower, cost
money, and could be wrong. An unmatched topic escalates to the front desk
rather than being improvised (docs/call-flows.md).

No verification: none of this is patient-specific.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.agents.state import SessionState
from app.config.clinic import lookup_faq
from app.schemas.domain import EscalationCategory
from app.services.escalation_service import EscalationService
from app.workflows.base import WorkflowResponse, WorkflowStatus

WORKFLOW_NAME = "clinic_faq"


class ClinicFaqInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    utterance: str = ""
    topic: str | None = None


class ClinicFaqWorkflow:
    """Answers clinic questions from structured data."""

    name = WORKFLOW_NAME

    def __init__(self, escalations: EscalationService) -> None:
        self.escalations = escalations

    async def advance(self, session: SessionState, turn: ClinicFaqInput) -> WorkflowResponse:
        answer = lookup_faq(turn.topic) if turn.topic else None

        if answer is None:
            escalation = self.escalations.create(
                category=EscalationCategory.ADMINISTRATIVE,
                summary=f"Clinic question not covered by the FAQ: {turn.topic!r}",
                session_id=session.session_id,
                patient_ref=session.patient_ref,
                verification_state=session.verification,
                patient_question=turn.utterance or None,
                ai_action="No answer given; question outside the clinic FAQ",
            )
            session.active_workflow = None
            return WorkflowResponse(
                workflow=self.name,
                state="ESCALATED",
                status=WorkflowStatus.ESCALATED,
                message=(
                    "I don't have that to hand, and I don't want to guess. Let me pass "
                    "you to our front desk."
                ),
                escalation_id=escalation.escalation_id,
            )

        session.active_workflow = None
        return WorkflowResponse(
            workflow=self.name,
            state="ANSWERED",
            status=WorkflowStatus.COMPLETED,
            message=answer,
        )
