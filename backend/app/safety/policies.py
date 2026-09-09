"""The safety classifier.

Runs **before** intent detection and before workflow dispatch, so there is no
path from an utterance to a workflow that skips it. Classification depends only
on the utterance and on server-side session facts -- never on anything the
model concluded -- which is what makes it non-promptable.

Two layers, deliberately asymmetric:

1. deterministic rules, which can refuse on their own, and
2. an optional model-assisted flag, which may *add* a refusal for a phrasing
   the rules missed but may never remove one.

Neither layer can grant permission the other denied. Ambiguity defaults to
refusing.
"""

from __future__ import annotations

import re

from app.observability.logging import get_logger
from app.safety.emergency_rules import EMERGENCY_MESSAGE, check_emergency
from app.safety.medication_rules import (
    DOSE_MESSAGE,
    SIDE_EFFECT_MESSAGE,
    check_dose_modification,
    check_side_effect,
)
from app.safety.models import (
    ALLOWED,
    SafetyCategory,
    SafetyContext,
    SafetyDecision,
    SafetyOutcome,
    refusal,
)
from app.safety.triage import (
    DIAGNOSIS_MESSAGE,
    HUMAN_MESSAGE,
    TREATMENT_MESSAGE,
    check_diagnosis,
    check_human_requested,
    check_treatment,
)
from app.schemas.domain import VerificationState

logger = get_logger(__name__)

#: Below this, the orchestrator does not trust its own reading of the request.
DEFAULT_CONFIDENCE_FLOOR = 0.55

VERIFICATION_FAILED_MESSAGE = (
    "I haven't been able to confirm your identity, so I can't access any records. I'm "
    "passing you to our front desk, who can help verify who you are."
)

UNCERTAIN_MESSAGE = (
    "I'm not confident I've understood that correctly, and I don't want to guess. Let me "
    "pass you to a member of our staff."
)

INCONSISTENT_RECORDS_MESSAGE = (
    "Something doesn't look right in what I can see on file, and I don't want to give you "
    "the wrong information. I'm passing this to our staff to check."
)

#: Evaluated in this order; the first match decides. Urgent symptoms outrank
#: everything, so "chest pain -- should I take more?" is an emergency, not a
#: dosing question.
MODEL_FLAG_MESSAGES: dict[SafetyCategory, str] = {
    SafetyCategory.URGENT_SYMPTOMS: EMERGENCY_MESSAGE,
    SafetyCategory.DOSE_MODIFICATION: DOSE_MESSAGE,
    SafetyCategory.MEDICATION_SIDE_EFFECT: SIDE_EFFECT_MESSAGE,
    SafetyCategory.DIAGNOSIS_REQUEST: DIAGNOSIS_MESSAGE,
    SafetyCategory.TREATMENT_REQUEST: TREATMENT_MESSAGE,
    SafetyCategory.HUMAN_REQUESTED: HUMAN_MESSAGE,
}

#: Evaluated in this order; the first match decides.
TEXT_RULES = (
    check_emergency,
    check_dose_modification,
    check_side_effect,
    check_diagnosis,
    check_treatment,
    check_human_requested,
)


# --------------------------------------------------------------------------
# Refusal content guard
# --------------------------------------------------------------------------

#: Dosage-shaped and instructional content. A refusal containing any of this
#: would be the very advice being refused.
_MEDICAL_INSTRUCTION = re.compile(
    r"\b\d+\s*(mg|mcg|ml|g|units?|tablets?|pills?|capsules?|puffs?|drops?)\b"
    r"|\btake \d+"
    r"|\b(double|halve|increase|decrease|reduce|lower|raise)\s+(your|the|my)?\s*"
    r"(dose|dosage|medication)"
    r"|\byou (should|need to|ought to|can) (take|stop taking|start taking|skip)"
    r"|\bstop taking\b"
    r"|\btwice daily\b|\bonce daily\b|\bwith meals\b",
    re.IGNORECASE,
)


def contains_medical_instruction(text: str) -> bool:
    """Whether text reads as medical instruction.

    Used to police **refusals**, which must carry no medical content at all.

    Not applied to every response: reading a stored dosage back to a patient is
    explicitly allowed (SAFETY.md category A) and will match this pattern by
    design. That path is governed by ``MedicationService.dosage_is_verbatim``
    instead, which checks the text still matches the record exactly.
    """
    return bool(_MEDICAL_INSTRUCTION.search(text))


# --------------------------------------------------------------------------
# Classifier
# --------------------------------------------------------------------------


class SafetyClassifier:
    """Decides whether a request may proceed."""

    def __init__(self, confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR) -> None:
        self.confidence_floor = confidence_floor

    def classify(
        self,
        utterance: str,
        context: SafetyContext | None = None,
        model_flag: SafetyCategory | None = None,
    ) -> SafetyDecision:
        """Rule on one utterance.

        Args:
            utterance: exactly what the caller said. Treated as data, never as
                instruction -- text asking the system to relax its rules is
                just text.
            context: server-side session facts.
            model_flag: an optional category proposed by a model for a phrasing
                the rules missed. It can only add a refusal.
        """
        ctx = context or SafetyContext()
        decision = self._deterministic(utterance, ctx)

        if decision.is_refusal:
            # A model may not overturn a deterministic refusal, so there is
            # nothing left to consider.
            self._log(decision, ctx)
            return decision

        if model_flag is not None:
            decision = self._from_model_flag(model_flag)

        self._log(decision, ctx)
        return decision

    # ------------------------------------------------------------ internals
    def _deterministic(self, utterance: str, ctx: SafetyContext) -> SafetyDecision:
        for rule in TEXT_RULES:
            result = rule(utterance)
            if result is not None:
                return result

        if ctx.verification_state is VerificationState.FAILED:
            return refusal(
                category=SafetyCategory.VERIFICATION_FAILED,
                rule="context.verification_failed",
                message=VERIFICATION_FAILED_MESSAGE,
                rationale="session verification has failed",
            )

        if ctx.records_inconsistent:
            return refusal(
                category=SafetyCategory.INCONSISTENT_RECORDS,
                rule="context.inconsistent_records",
                message=INCONSISTENT_RECORDS_MESSAGE,
                rationale="conflicting or missing records for a verified patient",
            )

        if ctx.confidence is not None and ctx.confidence < self.confidence_floor:
            return refusal(
                category=SafetyCategory.LOW_CONFIDENCE,
                rule="context.low_confidence",
                message=UNCERTAIN_MESSAGE,
                rationale=(
                    f"confidence {ctx.confidence:.2f} below floor {self.confidence_floor:.2f}"
                ),
            )

        return ALLOWED

    @staticmethod
    def _from_model_flag(category: SafetyCategory) -> SafetyDecision:
        return refusal(
            category=category,
            rule=f"model_flagged.{category.value}",
            message=MODEL_FLAG_MESSAGES.get(category, UNCERTAIN_MESSAGE),
            rationale="model flagged a phrasing the deterministic rules did not match",
        )

    @staticmethod
    def _log(decision: SafetyDecision, ctx: SafetyContext) -> None:
        if decision.outcome is SafetyOutcome.ALLOW:
            return
        logger.warning(
            "safety_refusal",
            category=decision.category.value if decision.category else None,
            matched_rule=decision.matched_rule,
            priority=decision.priority.value if decision.priority else None,
            verification=ctx.verification_state.value,
            patient_ref=ctx.patient_ref,
        )
