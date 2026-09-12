"""Questions the agent asks that are not part of a workflow.

Two kinds, and both used to go nowhere.

A workflow that has finished can still put a question: "would you like to book
one?", "would you like to rebook now?". It has closed itself by then, so there
is nothing left to hear the answer, and "yes please" reached the capability
menu -- the agent asking a question and forgetting it had asked.

And when nothing can answer a turn at all, the menu is a reasonable first reply
and a poor second one. Somebody who has asked twice for something the clinic
line does not do is not helped by hearing the list again; they need a person,
which is the one thing the agent can always offer.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from tests.conftest import JOHN_SMITH_DOB, SEED_NOW

from app.agents.extraction import ExtractedTurn, ExtractionContext, RuleBasedExtractor
from app.agents.factory import Runtime, build_runtime
from app.agents.intents import Intent
from app.agents.orchestrator import (
    ANYTHING_ELSE,
    DECLINED_MESSAGE,
    FALLBACK_MESSAGE,
    GOODBYE_MESSAGE,
    HANDOVER_MESSAGE,
    MORE_HELP_MESSAGE,
    OUT_OF_SCOPE_MESSAGE,
    STUCK_MESSAGE,
)
from app.agents.state import SessionState
from app.config.settings import Settings
from app.ehr.base import EHRProvider

IDENTIFY = f"John Smith, born {JOHN_SMITH_DOB:%d %B %Y}"


@pytest.fixture
def runtime(memory_ehr: EHRProvider) -> Runtime:
    return build_runtime(ehr=memory_ehr, settings=Settings(_env_file=None, app_env="test"))


@pytest.fixture
def session() -> SessionState:
    return SessionState(session_id="sess-offer", created_at=SEED_NOW)


async def say(runtime: Runtime, session: SessionState, *lines: str) -> str:
    message = ""
    for line in lines:
        message = (await runtime.orchestrator.handle_turn(session, line, now=SEED_NOW)).message
    return message


class TestSayingYesToAnOffer:
    async def test_rebooking_after_a_cancellation(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        """The offer is made by a workflow that then closes itself."""
        cancelled = await say(runtime, session, "I need to cancel my appointment", IDENTIFY, "Yes")
        assert "Would you like to rebook now?" in cancelled

        accepted = await say(runtime, session, "Yes please")

        assert accepted != FALLBACK_MESSAGE
        assert session.active_workflow == "existing_patient_booking"

    async def test_booking_when_there_was_nothing_to_look_up(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        await say(runtime, session, "I need to cancel my appointment", IDENTIFY, "Yes")
        nothing = await say(runtime, session, "When is my appointment?")
        assert "Would you like to book one?" in nothing

        accepted = await say(runtime, session, "Yes")

        assert accepted != FALLBACK_MESSAGE
        assert session.active_workflow == "existing_patient_booking"

    async def test_the_reason_they_give_next_is_heard(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        """The question after "yes" is what the visit is for.

        Mid-workflow turns classify as UNKNOWN by design, and the reason was
        only read off a turn that classified as a booking -- so the answer to
        "what would you like to be seen about?" was not a reason, and the
        workflow asked again, and again.
        """
        await say(
            runtime, session, "I need to cancel my appointment", IDENTIFY, "Yes", "Yes please"
        )

        offered = await say(runtime, session, "My knee has been hurting")

        assert "I have these times" in offered

    async def test_saying_no_does_not_start_anything(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        await say(runtime, session, "I need to cancel my appointment", IDENTIFY, "Yes")

        declined = await say(runtime, session, "No thanks")

        assert declined == DECLINED_MESSAGE
        assert session.active_workflow is None

    async def test_changing_the_subject_is_not_an_answer(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        """ "What are your opening hours?" is neither yes nor no.

        Treating anything that is not a yes as a refusal would swallow the
        question they actually asked.
        """
        await say(runtime, session, "I need to cancel my appointment", IDENTIFY, "Yes")

        changed = await say(runtime, session, "What time do you shut?")

        assert "8:00 AM to 5:00 PM" in changed


class TestOfferingAPerson:
    async def test_the_menu_comes_first(self, runtime: Runtime, session: SessionState) -> None:
        """It is a fair answer to a greeting or a false start."""
        assert await say(runtime, session, "Hello?") == FALLBACK_MESSAGE

    async def test_asking_twice_for_something_we_cannot_do_offers_a_person(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        await say(runtime, session, "Can you send my records to my solicitor?")

        second = await say(runtime, session, "I need my notes sent somewhere")

        assert second == OUT_OF_SCOPE_MESSAGE

    async def test_accepting_the_offer_escalates(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        await say(runtime, session, "Send my notes to my solicitor", "Send my notes somewhere")

        accepted = await say(runtime, session, "Yes please")

        assert accepted == HANDOVER_MESSAGE
        assert len(runtime.escalations.store.all()) == 1

    async def test_declining_it_does_not(self, runtime: Runtime, session: SessionState) -> None:
        await say(runtime, session, "Send my notes to my solicitor", "Send my notes somewhere")

        declined = await say(runtime, session, "No, it's fine")

        assert declined == DECLINED_MESSAGE
        assert not runtime.escalations.store.all()

    async def test_a_turn_that_worked_clears_the_count(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        """Two failures either side of a good answer are not a pattern."""
        await say(runtime, session, "Send my notes to my solicitor")
        await say(runtime, session, "What time do you shut?")

        after = await say(runtime, session, "Send my notes somewhere")

        assert after == FALLBACK_MESSAGE


class TestSayingTheSameThingTwice:
    """The last line of defence, and the only one that needs no diagnosis.

    Every loop found on a live call looked identical from the caller's side:
    the same sentence, again. A workflow left in a finished state, a choice
    nothing could resolve, an answer the rules could not parse -- different
    causes, one symptom. So the symptom is what is watched, which means this
    also catches the loops nobody has found yet.
    """

    async def test_a_third_identical_reply_offers_a_person(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        # The question is asked once by the look-up itself, and again when
        # "the one for my chest" resolves to nothing.
        asked_once = await say(runtime, session, "What does my prescription say?", IDENTIFY)
        assert "Which medication did you mean?" in asked_once

        asked_twice = await say(runtime, session, "the one for my chest")
        a_third_time = await say(runtime, session, "the one for my chest")

        assert asked_twice == asked_once, "the fixture no longer produces a loop"
        assert a_third_time == STUCK_MESSAGE

    async def test_and_then_hands_over(self, runtime: Runtime, session: SessionState) -> None:
        await say(runtime, session, "What does my prescription say?", IDENTIFY)
        for _ in range(2):
            await say(runtime, session, "the one for my chest")

        assert await say(runtime, session, "yes please") == HANDOVER_MESSAGE

    async def test_a_reply_that_changes_does_not_count(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        """Re-asking once is normal, and often works."""
        await say(runtime, session, "What does my prescription say?", IDENTIFY)
        await say(runtime, session, "the one for my chest")

        answered = await say(runtime, session, "Metformin")

        assert "One tablet twice daily" in answered
        assert session.workflow_state.get("_reply_repeats") == 1


class TestChangingTheSubject:
    """ "Actually, could you sort out my repeat while I'm on?"

    A workflow owns every turn until it finishes, which is what stops "yes"
    restarting a booking -- and also what answered a completely different
    request with the question asked before it, forever.

    Only the model may trigger this, and only about an intent it also named:
    telling a change of subject from a clumsy answer is the judgement the
    rules are worst at, and being wrong throws away a booking half made.
    """

    @pytest.fixture
    def moving_on(self, runtime: Runtime) -> Runtime:
        runtime.orchestrator.extractor = _Scripted(
            {"actually, sort out my repeat": (Intent.REFILL_REQUEST, True)}
        )
        return runtime

    async def test_it_puts_down_what_it_was_doing(
        self, moving_on: Runtime, session: SessionState
    ) -> None:
        await say(moving_on, session, "I'd like to book a follow up", IDENTIFY)
        assert session.active_workflow == "existing_patient_booking"

        moved = await say(moving_on, session, "actually, sort out my repeat")

        assert session.active_workflow == "refill_request"
        assert "refilled" in moved

    async def test_the_abandoned_workflow_keeps_nothing(
        self, moving_on: Runtime, session: SessionState
    ) -> None:
        """Coming back to it later must not resume against stale times."""
        await say(moving_on, session, "I'd like to book a follow up", IDENTIFY)
        assert session.workflow_state.get("existing_patient_booking.offers")

        await say(moving_on, session, "actually, sort out my repeat")

        assert not session.workflow_state.get("existing_patient_booking.offers")
        assert not session.workflow_state.get("existing_patient_booking.state")

    async def test_the_rules_alone_never_trigger_it(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        """Mock mode is rules-only, and a booking in progress stays one."""
        await say(runtime, session, "I'd like to book a follow up", IDENTIFY)

        await say(runtime, session, "actually, sort out my repeat")

        assert session.active_workflow == "existing_patient_booking"

    async def test_the_same_workflow_is_not_a_change(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        """ "No, cancel it instead" belongs to the workflow already running."""
        runtime.orchestrator.extractor = _Scripted(
            {"cancel it instead": (Intent.CANCEL_APPOINTMENT, True)}
        )
        await say(runtime, session, "When is my appointment?", IDENTIFY)
        await say(runtime, session, "I'd like to move it")

        await say(runtime, session, "cancel it instead")

        assert session.active_workflow == "appointment_management"


class _Scripted:
    """A rules extractor that reports a change of subject for named phrases.

    Standing in for the model, which is the only thing that can set it. The
    rules are still what parse the turn, so everything else about these tests
    is the real behaviour.
    """

    def __init__(self, moves: dict[str, tuple[Intent, bool]]) -> None:
        self.rules = RuleBasedExtractor()
        self.moves = moves

    def extract(self, utterance: str, context: ExtractionContext) -> ExtractedTurn:
        return self.rules.extract(utterance, context)

    async def aextract(self, utterance: str, context: ExtractionContext) -> ExtractedTurn:
        baseline = self.rules.extract(utterance, context)
        move = self.moves.get(utterance.lower())
        if move is None:
            return baseline
        intent, changed = move
        return replace(baseline, intent=intent, confidence=0.9, changes_subject=changed)


class TestEndingTheCall:
    """A finished request asks whether there is another, and takes no for an answer.

    The question was already there on some replies and missing from others,
    and nothing anywhere could hear the answer: "no, that's everything" landed
    on the capability menu, which is a rude way to end a call and leaves the
    line open for the silence timer to close instead.
    """

    async def test_a_completed_request_asks(self, runtime: Runtime, session: SessionState) -> None:
        answered = await say(
            runtime, session, "What does my prescription say?", IDENTIFY, "Metformin"
        )

        assert "One tablet twice daily" in answered
        assert ANYTHING_ELSE in answered

    async def test_no_ends_the_call(self, runtime: Runtime, session: SessionState) -> None:
        await say(runtime, session, "Are you open on Saturday?")

        goodbye = await say(runtime, session, "No, that's everything thanks")

        assert goodbye == GOODBYE_MESSAGE
        assert not session.is_active, "the line was left open after the caller finished"

    @pytest.mark.parametrize(
        "utterance",
        ["No thanks", "That's all, thanks", "Nope, that's it", "Nothing else", "No, I'm done"],
    )
    async def test_the_ways_people_say_it(
        self, runtime: Runtime, session: SessionState, utterance: str
    ) -> None:
        await say(runtime, session, "Are you open on Saturday?")

        assert await say(runtime, session, utterance) == GOODBYE_MESSAGE

    async def test_yes_does_not_read_the_menu_back(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        """They have just used the agent. They know what it does."""
        await say(runtime, session, "Are you open on Saturday?")

        assert await say(runtime, session, "Yes actually") == MORE_HELP_MESSAGE
        assert session.is_active

    async def test_saying_the_next_thing_instead_of_yes_works(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        """Most callers never answer the question; they just carry on."""
        await say(runtime, session, "Are you open on Saturday?")

        answer = await say(runtime, session, "Where are you?")

        assert "Oakwood Avenue" in answer
        assert session.is_active

    async def test_an_escalation_is_not_asked(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        """The call is on its way to a person; there is nothing else to offer."""
        answer = await say(runtime, session, "Can I speak to someone please?")

        assert ANYTHING_ELSE not in answer

    async def test_a_workflow_with_its_own_question_keeps_it(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        """A cancellation offers to rebook, and that offer must survive."""
        cancelled = await say(runtime, session, "I need to cancel my appointment", IDENTIFY, "Yes")

        assert "Would you like to rebook now?" in cancelled
        assert ANYTHING_ELSE not in cancelled
