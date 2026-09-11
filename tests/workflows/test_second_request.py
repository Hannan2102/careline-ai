"""One call, several requests.

Callers do not hang up and ring back between questions. They book, then check
it, then move it, then ask what they are meant to be taking -- and every one of
those is a fresh request against a workflow that has already finished one.

The bug this file exists for was found on a live call. The caller booked an
appointment, checked the time, then asked to move it, and was answered with
"What would you like to do with your appointment?" -- six times, whatever they
said next. The workflow's memory still held the state of the request that had
already finished, so it declined to re-initialise and fell through to its own
catch-all prompt, and starting it marked it active again, which routed every
later turn straight back into the same dead state.

What must be true afterwards is that a finished request starts over cleanly:
new state, and none of the old one's cached data, because acting on a cached
appointment that has since been cancelled acts on something that is gone.
"""

from __future__ import annotations

import pytest
from tests.conftest import JOHN_SMITH_DOB, SEED_NOW

from app.agents.factory import build_runtime
from app.agents.orchestrator import FALLBACK_MESSAGE, Orchestrator
from app.agents.state import SessionState
from app.config.settings import Settings
from app.ehr.base import EHRProvider

IDENTIFY = f"John Smith, born {JOHN_SMITH_DOB:%d %B %Y}"


@pytest.fixture
def orchestrator(memory_ehr: EHRProvider) -> Orchestrator:
    return build_runtime(
        ehr=memory_ehr, settings=Settings(_env_file=None, app_env="test")
    ).orchestrator


async def say(orchestrator: Orchestrator, session: SessionState, *lines: str) -> str:
    """Say each line in turn; return what the agent said to the last one."""
    message = ""
    for line in lines:
        message = (await orchestrator.handle_turn(session, line, now=SEED_NOW)).message
    return message


@pytest.fixture
def verified() -> SessionState:
    return SessionState(session_id="sess-second", created_at=SEED_NOW)


class TestASecondRequestIsHeard:
    async def test_a_booking_then_a_check_then_a_reschedule(
        self, orchestrator: Orchestrator, verified: SessionState
    ) -> None:
        """The live call, in order.

        The check in the middle is not padding -- it is the precondition. It
        is what leaves the appointment workflow finished-but-remembered, and
        without it the reschedule finds an empty memory and works even on the
        broken version.
        """
        await say(
            orchestrator,
            verified,
            "I need to see someone",
            IDENTIFY,
            "The first one, please",
            "Yes",
        )
        assert verified.workflow_state.get("existing_patient_booking.state") == "BOOKED"

        # Answered to completion, which is the state that used to be fatal.
        # (The caller now has two appointments, so the check asks which.)
        await say(orchestrator, verified, "What time is my appointment?", "The first one")
        assert verified.workflow_state.get("appointment_management.state") == "ANSWERED"

        moved = await say(orchestrator, verified, "Can we push it back a week?")

        assert "What would you like to do with your appointment?" not in moved
        assert moved != FALLBACK_MESSAGE

    async def test_a_lookup_then_a_cancellation(
        self, orchestrator: Orchestrator, verified: SessionState
    ) -> None:
        """Two requests through the same workflow, back to back."""
        first = await say(orchestrator, verified, "When is my appointment?", IDENTIFY)
        assert "Your next appointment" in first or "which one" in first.lower()

        second = await say(orchestrator, verified, "I need to cancel it")
        assert "What would you like to do with your appointment?" not in second

    async def test_a_second_medication_question(
        self, orchestrator: Orchestrator, verified: SessionState
    ) -> None:
        await say(orchestrator, verified, "What does my prescription say?", IDENTIFY)
        answered = await say(orchestrator, verified, "How much metformin do I take?")
        assert "Metformin" in answered
        assert answered != FALLBACK_MESSAGE

    async def test_a_refill_after_a_lookup(
        self, orchestrator: Orchestrator, verified: SessionState
    ) -> None:
        """Different workflows, and the second one must not re-verify."""
        await say(orchestrator, verified, "What does my prescription say?", IDENTIFY)
        refill = await say(orchestrator, verified, "I'm running low on my metformin")
        assert "date of birth" not in refill.lower(), "asked a verified caller to verify again"


class TestAVerifiedCallerIsNotAskedAgain:
    """Verification is session-scoped, so it survives a change of subject.

    Each of these enters its workflow for the first time on a session another
    workflow has already verified -- the case a workflow that assumes it is
    starting from nothing gets wrong.
    """

    @pytest.mark.parametrize(
        ("opening", "expected_absent"),
        [
            ("Am I covered?", "date of birth"),
            ("I need an appointment next week", "date of birth"),
            ("What am I meant to be taking?", "date of birth"),
            ("I'm running low", "date of birth"),
        ],
    )
    async def test_the_second_subject_needs_no_identity(
        self,
        orchestrator: Orchestrator,
        verified: SessionState,
        opening: str,
        expected_absent: str,
    ) -> None:
        await say(orchestrator, verified, "When is my appointment?", IDENTIFY)
        assert verified.is_verified

        answer = await say(orchestrator, verified, opening)
        assert expected_absent not in answer.lower(), (
            f"{opening!r} asked a verified caller to prove who they are again"
        )


class TestTheOldRequestIsNotReused:
    async def test_an_appointment_cancelled_once_is_not_offered_again(
        self, orchestrator: Orchestrator, verified: SessionState
    ) -> None:
        """The reason a finished request is cleared rather than merely reset.

        The list of appointments was fetched for the request that has just
        ended. After a cancellation it names something that no longer exists,
        and a workflow that kept it would cheerfully offer to cancel it twice.
        """
        await say(orchestrator, verified, "I need to cancel my appointment", IDENTIFY)
        confirmed = await say(orchestrator, verified, "Yes")
        assert "cancelled" in confirmed.lower()

        again = await say(orchestrator, verified, "Cancel my appointment")
        assert "don't see any upcoming appointments" in again
