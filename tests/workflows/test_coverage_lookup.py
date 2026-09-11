"""Reading insurance cover back to a verified caller.

The three outcomes are the point. "Covered", "the plan we have has expired" and
"nothing on file" are different answers to the same question, and collapsing
any two of them misinforms somebody holding a card.

What this must never do is quote a price. The clinic records which plan was
presented; it does not hold the deductible or the negotiated rate, and a
confident figure that turns out to be wrong is worse than no figure at all.
"""

from __future__ import annotations

from datetime import date

import pytest
from tests.conftest import JOHN_SMITH, JOHN_SMITH_DOB, SEED_NOW

from app.agents.factory import build_runtime
from app.agents.orchestrator import Orchestrator
from app.agents.state import SessionState
from app.config.settings import Settings
from app.ehr.base import EHRProvider
from app.schemas.domain import AuditAction
from app.services.coverage_service import CoverageService, CoverageStatus

ROBERT_A = "Patient/demo-robert-johnson-a"
ROBERT_B = "Patient/demo-robert-johnson-b"


@pytest.fixture
def orchestrator(memory_ehr: EHRProvider) -> Orchestrator:
    return build_runtime(
        ehr=memory_ehr, settings=Settings(_env_file=None, app_env="test")
    ).orchestrator


class TestTheService:
    async def test_active_cover(self, memory_ehr: EHRProvider) -> None:
        result = await CoverageService(memory_ehr).for_patient(JOHN_SMITH, today=SEED_NOW.date())
        assert result.status is CoverageStatus.ACTIVE
        assert result.coverage is not None
        assert result.coverage.plan_name == "Blue Shield PPO (demo)"

    async def test_cancelled_cover_reads_as_lapsed(self, memory_ehr: EHRProvider) -> None:
        result = await CoverageService(memory_ehr).for_patient(ROBERT_A, today=SEED_NOW.date())
        assert result.status is CoverageStatus.LAPSED
        assert result.coverage is not None
        assert result.coverage.plan_name == "Statewide Medicaid (demo)"

    async def test_nothing_on_file(self, memory_ehr: EHRProvider) -> None:
        result = await CoverageService(memory_ehr).for_patient(ROBERT_B, today=SEED_NOW.date())
        assert result.status is CoverageStatus.NONE
        assert result.coverage is None

    async def test_a_plan_that_has_run_out_is_not_active(self, memory_ehr: EHRProvider) -> None:
        """Status and period are both checked, because either alone lies.

        A record can say "active" and have run out, or be cancelled with an end
        date still in the future. Trusting the flag alone tells a patient they
        are covered when they are not.
        """
        service = CoverageService(memory_ehr)
        long_after = await service.for_patient(JOHN_SMITH, today=date(2099, 1, 1))
        assert long_after.status is CoverageStatus.LAPSED


class TestTheConversation:
    async def test_a_verified_caller_hears_their_plan(self, orchestrator: Orchestrator) -> None:
        session = SessionState(session_id="sess-cov", created_at=SEED_NOW)
        await orchestrator.handle_turn(session, "What insurance do I have?", now=SEED_NOW)
        result = await orchestrator.handle_turn(
            session, f"John Smith, born {JOHN_SMITH_DOB:%d %B %Y}", now=SEED_NOW
        )
        assert "Blue Shield PPO (demo)" in result.message

    async def test_the_answer_refuses_to_quote_a_price(self, orchestrator: Orchestrator) -> None:
        """The one thing the record cannot support."""
        session = SessionState(session_id="sess-cov", created_at=SEED_NOW)
        await orchestrator.handle_turn(session, "Am I covered?", now=SEED_NOW)
        result = await orchestrator.handle_turn(
            session, f"John Smith, born {JOHN_SMITH_DOB:%d %B %Y}", now=SEED_NOW
        )
        assert "can't tell you what a visit will cost" in result.message
        assert "front desk" in result.message

    async def test_cover_is_not_disclosed_before_verification(
        self, orchestrator: Orchestrator
    ) -> None:
        """The plan name is PHI like any other field on the record."""
        session = SessionState(session_id="sess-cov", created_at=SEED_NOW)
        result = await orchestrator.handle_turn(session, "Am I covered?", now=SEED_NOW)
        assert "Blue Shield" not in result.message
        assert "confirm" in result.message.lower() or "name" in result.message.lower()

    async def test_asking_about_someone_else_discloses_nothing(
        self, orchestrator: Orchestrator
    ) -> None:
        session = SessionState(session_id="sess-cov", created_at=SEED_NOW)
        result = await orchestrator.handle_turn(
            session, "What insurance does John Smith have?", now=SEED_NOW
        )
        # The accepted-plans list names Blue Shield because the clinic takes
        # it, so the plan name alone proves nothing. What must not appear is a
        # claim about *this patient's* record.
        assert "you're covered by" not in result.message
        assert session.patient_ref is None, "read a record without verifying anyone"

    async def test_the_read_is_audited(self, orchestrator: Orchestrator) -> None:
        session = SessionState(session_id="sess-cov", created_at=SEED_NOW)
        await orchestrator.handle_turn(session, "Am I covered?", now=SEED_NOW)
        await orchestrator.handle_turn(
            session, f"John Smith, born {JOHN_SMITH_DOB:%d %B %Y}", now=SEED_NOW
        )
        actions = [e.action for e in orchestrator.audit.store.all()]
        assert AuditAction.COVERAGE_READ in actions


class TestWhoseInsurance:
    """The caller's cover and the clinic's accepted plans are different asks.

    They share almost every word, so the possessive decides. Getting it wrong in
    one direction makes a caller verify to hear a public fact; in the other it
    answers a question about their record with a brochure.
    """

    @pytest.mark.parametrize(
        "question",
        [
            "What insurance do I have?",
            "Am I covered?",
            "What is my plan?",
            "Is my insurance on file?",
        ],
    )
    async def test_personal_questions_require_verification(
        self, orchestrator: Orchestrator, question: str
    ) -> None:
        session = SessionState(session_id="sess-cov", created_at=SEED_NOW)
        result = await orchestrator.handle_turn(session, question, now=SEED_NOW)
        assert "We accept" not in result.message, "answered a record question with the brochure"

    @pytest.mark.parametrize(
        "question",
        [
            "What insurance do you accept?",
            "Do you take Blue Shield?",
            "Do you take my insurance?",
        ],
    )
    async def test_clinic_questions_need_no_verification(
        self, orchestrator: Orchestrator, question: str
    ) -> None:
        session = SessionState(session_id="sess-cov", created_at=SEED_NOW)
        result = await orchestrator.handle_turn(session, question, now=SEED_NOW)
        assert "We accept" in result.message, "made the caller verify to hear a public fact"


class TestBilling:
    """Money goes to the front desk, and never through a record."""

    @pytest.mark.parametrize(
        "question",
        [
            "How much will this cost me?",
            "What's my copay?",
            "What is the cost of a visit?",
            "How much do you charge?",
        ],
    )
    async def test_cost_questions_are_answered_without_asking_who_you_are(
        self, orchestrator: Orchestrator, question: str
    ) -> None:
        """A billing question used to open a PHI-gated identity flow.

        "How much" is how you ask about a dosage, so a question about price
        routed to the medication lookup and demanded a name and date of birth
        before it would say anything.
        """
        session = SessionState(session_id="sess-bill", created_at=SEED_NOW)
        result = await orchestrator.handle_turn(session, question, now=SEED_NOW)
        assert "date of birth" not in result.message.lower(), (
            "a question about money asked the caller to prove who they are"
        )
        assert "front desk" in result.message

    async def test_a_dosage_question_still_reaches_the_medication_lookup(
        self, orchestrator: Orchestrator
    ) -> None:
        """ "How much" belongs to both, and medications must keep it."""
        session = SessionState(session_id="sess-dose", created_at=SEED_NOW)
        result = await orchestrator.handle_turn(
            session, "How much Metformin do I take?", now=SEED_NOW
        )
        assert "date of birth" in result.message.lower()
