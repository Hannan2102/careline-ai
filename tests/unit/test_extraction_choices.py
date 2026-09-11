"""Resolving which offer the caller picked, and which clinic question they asked.

Both of these were found by reading transcripts rather than by a failing test,
and both failed in the same shape: the extractor returned a confident answer
that happened to be the wrong one. That is worse than returning nothing --
the caller hears a confirmation naming a time they did not choose.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.agents.extraction import ExtractionContext, RuleBasedExtractor
from app.agents.intents import Intent
from app.workflows.base import SlotOffer

T0 = datetime(2026, 9, 11, 8, 0, tzinfo=UTC)

OFFERS = tuple(
    SlotOffer(
        index=index,
        slot_id=f"slot-{index}",
        practitioner_ref="Practitioner/demo-patel",
        practitioner_name="Dr. Sarah Patel",
        start=start,
        duration_minutes=30,
    )
    for index, start in enumerate(
        (
            datetime(2026, 9, 11, 8, 0, tzinfo=UTC),
            datetime(2026, 9, 14, 8, 0, tzinfo=UTC),
            datetime(2026, 9, 15, 8, 0, tzinfo=UTC),
        ),
        start=1,
    )
)


@pytest.fixture
def extractor() -> RuleBasedExtractor:
    return RuleBasedExtractor()


@pytest.fixture
def offered() -> ExtractionContext:
    return ExtractionContext(offers=OFFERS)


class TestSlotChoice:
    """ "The second one" must mean the second one.

    It did not. The ordinal table was scanned in insertion order, so the `one`
    entry matched the trailing pronoun in "the second one" and returned 1
    before `second` was ever tested. Every "the Nth one" phrasing -- the most
    natural way to pick from a spoken list -- booked the first slot.
    """

    @pytest.mark.parametrize(
        ("utterance", "expected"),
        [
            ("The first one", 1),
            ("The second one", 2),
            ("The third one", 3),
            ("the 2nd one", 2),
            ("I'll take the second one please", 2),
            ("second", 2),
            ("number 2", 2),
            ("option 3", 3),
            ("three", 3),
            # A bare "one" really is the first: no ordinal precedes it.
            ("the one", 1),
            ("one", 1),
        ],
    )
    def test_resolves_the_ordinal_the_caller_said(
        self,
        extractor: RuleBasedExtractor,
        offered: ExtractionContext,
        utterance: str,
        expected: int,
    ) -> None:
        assert extractor.extract(utterance, offered).ordinal == expected

    def test_last_means_the_final_offer(
        self, extractor: RuleBasedExtractor, offered: ExtractionContext
    ) -> None:
        assert extractor.extract("The last one", offered).ordinal == len(OFFERS)

    def test_a_weekday_still_wins_over_an_ordinal_word(
        self, extractor: RuleBasedExtractor, offered: ExtractionContext
    ) -> None:
        """ "The Monday one" names a day, not a position.

        The weekday check runs first for exactly this reason, and the trailing
        "one" must not hijack it.
        """
        assert extractor.extract("the Monday one", offered).ordinal == 2

    def test_no_choice_is_still_no_choice(
        self, extractor: RuleBasedExtractor, offered: ExtractionContext
    ) -> None:
        """Guessing is what caused the bug; silence is the safe answer."""
        assert extractor.extract("hmm, let me think", offered).ordinal is None


class TestClinicQuestions:
    """Clinic questions reach the FAQ workflow, answerable or not."""

    @pytest.mark.parametrize(
        ("utterance", "topic"),
        [
            ("Where are you located?", "where_are_you"),
            ("What's your address?", "address"),
            ("Are you open on Saturday?", "saturday"),
            ("Is there parking?", "parking"),
            ("Do you take Blue Shield?", "blue_shield"),
            ("Do you take Medicare?", "medicare"),
            ("What insurance do you accept?", "insurance"),
        ],
    )
    def test_an_answerable_question_carries_its_topic(
        self, extractor: RuleBasedExtractor, utterance: str, topic: str
    ) -> None:
        extracted = extractor.extract(utterance, ExtractionContext())
        assert extracted.intent is Intent.CLINIC_FAQ
        assert extracted.faq_topic == topic

    @pytest.mark.parametrize(
        "utterance",
        [
            "Do you have a physiotherapist?",
            "Do you do x-rays?",
            "Do you take Aetna?",
            "Do you offer blood tests?",
            "Can I get a referral?",
        ],
    )
    def test_an_unanswerable_one_reaches_the_workflow_with_no_topic(
        self, extractor: RuleBasedExtractor, utterance: str
    ) -> None:
        """Which is what makes it escalate to the front desk.

        The FAQ workflow has always escalated a topic it cannot answer, but
        nothing could reach that branch: intent only became CLINIC_FAQ once a
        topic had matched, and every topic in the table has an answer. These
        questions used to be met with the generic list of things the agent can
        do, which reads as a non-answer.
        """
        extracted = extractor.extract(utterance, ExtractionContext())
        assert extracted.intent is Intent.CLINIC_FAQ
        assert extracted.faq_topic is None

    @pytest.mark.parametrize(
        "utterance",
        [
            "What can you help with?",
            "Do you have any appointments free?",
            "Can you book me in?",
            "I need a refill",
        ],
    )
    def test_it_does_not_swallow_everything_else(
        self, extractor: RuleBasedExtractor, utterance: str
    ) -> None:
        """The reason the clinic-question markers are a short list.

        A general "is this about the clinic?" rule would answer a question
        about the agent itself with a transfer to a human.
        """
        assert extractor.extract(utterance, ExtractionContext()).intent is not Intent.CLINIC_FAQ


class TestTheAgentUnderstandsItsOwnOffer:
    """Whatever the fallback advertises, the matcher must accept.

    Found in a live call. The agent's fallback says it can help with "what your
    prescription says"; the caller asked exactly that, four times, in four
    phrasings, and got the same fallback back each time. The trigger list had
    "my prescriptions" and not "my prescription", so the singular missed.

    Advertising a capability in words the matcher rejects is worse than not
    advertising it: the caller has been told what to say, and saying it does
    not work.
    """

    @pytest.mark.parametrize(
        "utterance",
        [
            "Can you tell me what my prescription says?",
            "Tell me what my prescription says.",
            "What does my prescription say about the asthma medicines?",
            "What medication am I on?",
            "How do I use my inhaler?",
        ],
    )
    def test_asking_what_the_prescription_says_reaches_the_lookup(
        self, extractor: RuleBasedExtractor, utterance: str
    ) -> None:
        assert extractor.extract(utterance, ExtractionContext()).intent is Intent.MEDICATION_LOOKUP

    @pytest.mark.parametrize(
        "utterance",
        [
            "I need a refill on my prescription",
            "Can I get a repeat prescription?",
            "I'm running out of my medication",
            "I need more of my inhaler",
        ],
    )
    def test_a_refill_is_still_a_refill(
        self, extractor: RuleBasedExtractor, utterance: str
    ) -> None:
        """Both intents now mention prescriptions, and only one reorders records.

        Refill is matched first on purpose. Asking what a prescription says is
        a read; asking for more of it writes a request a clinician must review.
        """
        assert extractor.extract(utterance, ExtractionContext()).intent is Intent.REFILL_REQUEST

    def test_the_fallback_wording_is_covered_by_the_matcher(self) -> None:
        """A guard against the two drifting apart again.

        If the fallback is reworded, the phrase it offers has to remain
        something this extractor recognises.
        """
        from app.agents.orchestrator import FALLBACK_MESSAGE

        assert "prescription" in FALLBACK_MESSAGE
        spoken_back = "Can you tell me what my prescription says?"
        assert extractor_intent(spoken_back) is Intent.MEDICATION_LOOKUP, (
            "the fallback offers wording the matcher does not accept"
        )


def extractor_intent(utterance: str) -> Intent:
    return RuleBasedExtractor().extract(utterance, ExtractionContext()).intent
