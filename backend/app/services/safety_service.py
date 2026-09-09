"""Safety evaluation with escalation.

Ties the classifier to the escalation record. Classification itself is pure;
creating the handoff is a side effect, so it lives in the service layer.

The handoff is the deliverable here. A refusal that does not reach a human is
just a dead end for the patient, and one that arrives without context makes
them start again.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.agents.state import SessionState
from app.observability.logging import get_logger
from app.safety.models import SafetyCategory, SafetyContext, SafetyDecision
from app.safety.policies import SafetyClassifier, contains_medical_instruction
from app.schemas.domain import Escalation
from app.services.escalation_service import EscalationService

logger = get_logger(__name__)

#: What the agent did, recorded on every clinical handoff so the reviewing
#: clinician knows nothing was advised before they saw it.
AI_ACTION_BY_CATEGORY: dict[SafetyCategory, str] = {
    SafetyCategory.DOSE_MODIFICATION: "No dosage recommendation provided",
    SafetyCategory.MEDICATION_SIDE_EFFECT: "No clinical advice provided",
    SafetyCategory.DIAGNOSIS_REQUEST: "No diagnosis offered",
    SafetyCategory.TREATMENT_REQUEST: "No treatment advice provided",
    SafetyCategory.URGENT_SYMPTOMS: "Directed caller to emergency services; no assessment made",
    SafetyCategory.HUMAN_REQUESTED: "Transferred at patient's request",
    SafetyCategory.VERIFICATION_FAILED: "No patient information disclosed",
    SafetyCategory.LOW_CONFIDENCE: "No action taken; request not understood",
    SafetyCategory.INCONSISTENT_RECORDS: "No information given from conflicting records",
}


class SafetyEvaluation(BaseModel):
    """The ruling plus the handoff it produced, if any."""

    model_config = ConfigDict(frozen=True)

    decision: SafetyDecision
    escalation: Escalation | None = None

    @property
    def is_refusal(self) -> bool:
        return self.decision.is_refusal

    @property
    def patient_message(self) -> str | None:
        return self.decision.patient_message


class SafetyService:
    """Classifies an utterance and escalates when it must be refused."""

    def __init__(
        self,
        classifier: SafetyClassifier | None = None,
        escalations: EscalationService | None = None,
    ) -> None:
        self.classifier = classifier or SafetyClassifier()
        self.escalations = escalations or EscalationService()

    def evaluate(
        self,
        utterance: str,
        session: SessionState,
        medication_display: str | None = None,
        confidence: float | None = None,
        records_inconsistent: bool = False,
        model_flag: SafetyCategory | None = None,
        now: datetime | None = None,
    ) -> SafetyEvaluation:
        """Classify, and create a handoff if the request is refused."""
        context = SafetyContext(
            verification_state=session.verification,
            patient_ref=session.patient_ref,
            confidence=confidence,
            records_inconsistent=records_inconsistent,
            medication_display=medication_display,
        )
        decision = self.classifier.classify(utterance, context, model_flag=model_flag)

        if not decision.requires_escalation:
            return SafetyEvaluation(decision=decision)

        assert decision.category is not None  # every refusal carries a category
        assert decision.escalation_category is not None

        escalation = self.escalations.create(
            category=decision.escalation_category,
            summary=self._summarise(decision, session, medication_display),
            session_id=session.session_id,
            patient_ref=session.patient_ref,
            verification_state=session.verification,
            # The patient's own words, unparaphrased: the clinician should see
            # what was actually said, not our reading of it.
            patient_question=utterance,
            medication_display=medication_display,
            ai_action=AI_ACTION_BY_CATEGORY[decision.category],
            priority=decision.priority,
            now=now,
        )
        return SafetyEvaluation(decision=decision, escalation=escalation)

    @staticmethod
    def _summarise(
        decision: SafetyDecision, session: SessionState, medication_display: str | None
    ) -> str:
        parts = [f"Refused: {decision.category.value if decision.category else 'unknown'}."]
        if medication_display:
            parts.append(f"Medication discussed: {medication_display}.")
        parts.append("Patient verified." if session.is_verified else "Patient not verified.")
        parts.append(f"Rule: {decision.matched_rule}.")
        return " ".join(parts)

    def refusal_is_safe(self, decision: SafetyDecision) -> bool:
        """Whether a refusal is free of medical content.

        A belt-and-braces check on the fixed refusal text: if a message is ever
        edited into something instructional, this catches it.
        """
        if decision.patient_message is None:
            return True
        return not contains_medical_instruction(decision.patient_message)
