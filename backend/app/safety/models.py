"""Safety decision types.

Kept separate from the rules and the engine so rule modules and the classifier
can share them without importing each other.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.schemas.domain import EscalationCategory, Priority, VerificationState


class SafetyCategory(StrEnum):
    """Why a request was refused. One per row of the SAFETY.md policy table."""

    DOSE_MODIFICATION = "dose_modification"
    MEDICATION_SIDE_EFFECT = "medication_side_effect"
    DIAGNOSIS_REQUEST = "diagnosis_request"
    TREATMENT_REQUEST = "treatment_request"
    URGENT_SYMPTOMS = "urgent_symptoms"
    VERIFICATION_FAILED = "verification_failed"
    HUMAN_REQUESTED = "human_requested"
    LOW_CONFIDENCE = "low_confidence"
    INCONSISTENT_RECORDS = "inconsistent_records"


class SafetyOutcome(StrEnum):
    ALLOW = "ALLOW"
    #: Permitted, but the workflow must respect a constraint -- reading a stored
    #: dosage verbatim rather than describing it, for example.
    ALLOW_WITH_CONSTRAINTS = "ALLOW_WITH_CONSTRAINTS"
    REFUSE_AND_ESCALATE = "REFUSE_AND_ESCALATE"


#: Fixed routing per category. Derived, never supplied, so a refusal cannot be
#: talked into a gentler destination.
CATEGORY_ROUTING: dict[SafetyCategory, tuple[EscalationCategory, Priority]] = {
    SafetyCategory.URGENT_SYMPTOMS: (EscalationCategory.CLINICAL, Priority.URGENT),
    SafetyCategory.DOSE_MODIFICATION: (EscalationCategory.CLINICAL, Priority.CLINICAL),
    SafetyCategory.MEDICATION_SIDE_EFFECT: (EscalationCategory.CLINICAL, Priority.CLINICAL),
    SafetyCategory.DIAGNOSIS_REQUEST: (EscalationCategory.CLINICAL, Priority.CLINICAL),
    SafetyCategory.TREATMENT_REQUEST: (EscalationCategory.CLINICAL, Priority.CLINICAL),
    SafetyCategory.HUMAN_REQUESTED: (
        EscalationCategory.PATIENT_REQUESTED,
        Priority.PATIENT_REQUESTED,
    ),
    SafetyCategory.VERIFICATION_FAILED: (
        EscalationCategory.FAILED_VERIFICATION,
        Priority.ADMINISTRATIVE,
    ),
    SafetyCategory.LOW_CONFIDENCE: (
        EscalationCategory.SYSTEM_UNCERTAINTY,
        Priority.SYSTEM_UNCERTAINTY,
    ),
    SafetyCategory.INCONSISTENT_RECORDS: (
        EscalationCategory.SYSTEM_UNCERTAINTY,
        Priority.SYSTEM_UNCERTAINTY,
    ),
}


class SafetyContext(BaseModel):
    """Session facts a rule may consider beyond the words themselves."""

    model_config = ConfigDict(frozen=True)

    verification_state: VerificationState = VerificationState.UNVERIFIED
    patient_ref: str | None = None
    #: The orchestrator's confidence in its own intent classification.
    confidence: float | None = None
    #: Set when a read returned conflicting or impossible records.
    records_inconsistent: bool = False
    #: Medication under discussion, for the handoff summary.
    medication_display: str | None = None


class SafetyDecision(BaseModel):
    """The ruling on one utterance.

    ``patient_message`` is the exact wording used when refusing. It is fixed
    text, not generated, so a refusal cannot drift into advice.
    """

    model_config = ConfigDict(frozen=True)

    outcome: SafetyOutcome
    category: SafetyCategory | None = None
    #: The named rule that fired, recorded in the audit trail so any refusal
    #: traces to a specific rule rather than to model discretion.
    matched_rule: str | None = None
    matched_text: str | None = None
    escalation_category: EscalationCategory | None = None
    priority: Priority | None = None
    patient_message: str | None = None
    #: Internal reasoning. Never spoken to the patient.
    rationale: str = ""

    @property
    def is_refusal(self) -> bool:
        return self.outcome is SafetyOutcome.REFUSE_AND_ESCALATE

    @property
    def requires_escalation(self) -> bool:
        return self.is_refusal


ALLOWED = SafetyDecision(outcome=SafetyOutcome.ALLOW, rationale="no safety rule matched")


def refusal(
    category: SafetyCategory,
    rule: str,
    message: str,
    matched_text: str | None = None,
    rationale: str = "",
) -> SafetyDecision:
    """Build a refusal with routing derived from the category."""
    escalation_category, priority = CATEGORY_ROUTING[category]
    return SafetyDecision(
        outcome=SafetyOutcome.REFUSE_AND_ESCALATE,
        category=category,
        matched_rule=rule,
        matched_text=matched_text,
        escalation_category=escalation_category,
        priority=priority,
        patient_message=message,
        rationale=rationale or f"matched rule {rule!r}",
    )
