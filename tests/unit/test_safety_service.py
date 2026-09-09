"""Safety evaluation and the structured handoff (Phase 8).

The handoff is the deliverable: a refusal that does not reach a human is a
dead end, and one arriving without context makes the patient start over.
"""

from __future__ import annotations

import pytest

from app.agents.state import SessionState, VerificationDecision
from app.safety.models import SafetyCategory
from app.safety.policies import contains_medical_instruction
from app.schemas.domain import EscalationCategory, Priority, VerificationState
from app.services.escalation_service import EscalationService
from app.services.safety_service import SafetyService

JOHN = "Patient/demo-john-smith"


@pytest.fixture
def escalations() -> EscalationService:
    return EscalationService()


@pytest.fixture
def safety(escalations: EscalationService) -> SafetyService:
    return SafetyService(escalations=escalations)


@pytest.fixture
def verified_session() -> SessionState:
    session = SessionState("sess-safety")
    session.apply_verification(
        VerificationDecision(state=VerificationState.VERIFIED, patient_ref=JOHN)
    )
    return session


class TestTheCanonicalScenario:
    """DEMO.md scenario 3, the one that matters most."""

    def test_dizziness_and_a_dose_question_produces_a_clinical_handoff(
        self, safety: SafetyService, verified_session: SessionState
    ) -> None:
        utterance = "My Lisinopril is making me dizzy. Should I take half instead?"

        result = safety.evaluate(utterance, verified_session, medication_display="Lisinopril 10 mg")

        assert result.is_refusal is True
        assert result.decision.category is SafetyCategory.DOSE_MODIFICATION

        escalation = result.escalation
        assert escalation is not None
        assert escalation.patient_ref == JOHN
        assert escalation.verification_state is VerificationState.VERIFIED
        assert escalation.medication_display == "Lisinopril 10 mg"
        assert escalation.patient_question == utterance
        assert escalation.ai_action == "No dosage recommendation provided"
        assert escalation.destination == "Nurse / clinical staff"
        assert escalation.priority is Priority.CLINICAL
        assert escalation.category is EscalationCategory.CLINICAL

    def test_the_patient_is_told_it_needs_clinical_review(
        self, safety: SafetyService, verified_session: SessionState
    ) -> None:
        result = safety.evaluate(
            "My Lisinopril is making me dizzy. Should I take half instead?",
            verified_session,
        )
        message = result.patient_message
        assert message is not None
        assert "clinical staff" in message
        assert not contains_medical_instruction(message)


class TestHandoffContents:
    def test_the_question_is_preserved_verbatim(
        self, safety: SafetyService, verified_session: SessionState
    ) -> None:
        """The clinician should see what was said, not our paraphrase of it."""
        utterance = "  should i JUST take half of the little white one??  "
        result = safety.evaluate(utterance, verified_session)
        assert result.escalation is not None
        assert result.escalation.patient_question == utterance

    def test_an_unverified_caller_is_recorded_as_unverified(self, safety: SafetyService) -> None:
        result = safety.evaluate("What's wrong with me?", SessionState("sess-x"))
        assert result.escalation is not None
        assert result.escalation.verification_state is VerificationState.UNVERIFIED
        assert result.escalation.patient_ref is None
        assert "not verified" in result.escalation.summary

    def test_urgent_symptoms_produce_an_urgent_escalation(
        self, safety: SafetyService, verified_session: SessionState
    ) -> None:
        result = safety.evaluate("I'm having chest pain", verified_session)
        assert result.escalation is not None
        assert result.escalation.priority is Priority.URGENT
        assert "emergency services" in result.escalation.ai_action

    def test_a_human_request_routes_to_the_front_desk(
        self, safety: SafetyService, verified_session: SessionState
    ) -> None:
        result = safety.evaluate("Can I speak to a real person?", verified_session)
        assert result.escalation is not None
        assert result.escalation.destination == "Front desk"
        assert result.escalation.category is EscalationCategory.PATIENT_REQUESTED

    def test_every_refusal_records_the_rule_that_fired(
        self, safety: SafetyService, verified_session: SessionState
    ) -> None:
        """Any refusal must trace to a named rule, not to model discretion."""
        for utterance in [
            "Should I double the dose?",
            "Do I have an infection?",
            "I'm having chest pain",
        ]:
            result = safety.evaluate(utterance, verified_session)
            assert result.decision.matched_rule is not None
            assert result.escalation is not None
            assert result.decision.matched_rule in result.escalation.summary


class TestAllowedRequests:
    def test_an_allowed_request_creates_no_escalation(
        self, safety: SafetyService, verified_session: SessionState, escalations: EscalationService
    ) -> None:
        result = safety.evaluate("When is my appointment?", verified_session)
        assert result.is_refusal is False
        assert result.escalation is None
        assert escalations.store.all() == []

    def test_a_dosage_lookup_is_allowed_through(
        self, safety: SafetyService, verified_session: SessionState
    ) -> None:
        """Reading back a stored instruction is SAFETY.md category A."""
        result = safety.evaluate(
            "I forgot how much Metformin I'm supposed to take", verified_session
        )
        assert result.is_refusal is False


class TestRefusalContentGuard:
    def test_every_categorys_refusal_is_free_of_medical_content(
        self, safety: SafetyService, verified_session: SessionState
    ) -> None:
        samples = {
            SafetyCategory.URGENT_SYMPTOMS: "I'm having chest pain",
            SafetyCategory.DOSE_MODIFICATION: "Should I take half?",
            SafetyCategory.MEDICATION_SIDE_EFFECT: "It makes me dizzy",
            SafetyCategory.DIAGNOSIS_REQUEST: "What's wrong with me?",
            SafetyCategory.TREATMENT_REQUEST: "What should I take for this?",
            SafetyCategory.HUMAN_REQUESTED: "Let me talk to a person",
        }
        for expected, utterance in samples.items():
            result = safety.evaluate(utterance, verified_session)
            assert result.decision.category is expected
            assert safety.refusal_is_safe(result.decision), (
                f"{expected.value} refusal contains medical content: "
                f"{result.decision.patient_message!r}"
            )


class TestEscalationRecords:
    def test_escalations_are_retrievable_per_session(
        self, safety: SafetyService, verified_session: SessionState, escalations: EscalationService
    ) -> None:
        safety.evaluate("Should I take half?", verified_session)
        safety.evaluate("I'm having chest pain", verified_session)

        for_session = escalations.store.for_session(verified_session.session_id)
        assert len(for_session) == 2
        assert {e.priority for e in for_session} == {Priority.CLINICAL, Priority.URGENT}

    def test_escalation_ids_are_unique(
        self, safety: SafetyService, verified_session: SessionState, escalations: EscalationService
    ) -> None:
        for _ in range(5):
            safety.evaluate("Should I take half?", verified_session)
        ids = {e.escalation_id for e in escalations.store.all()}
        assert len(ids) == 5
