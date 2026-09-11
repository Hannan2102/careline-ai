"""Everything a caller might say, and where it has to go.

One table per intent, built from the way people actually phrase things rather
than from the way the code happens to match them. It is a regression net for
the rules specifically -- not the model -- because the rules are the floor: a
rate-limited or unreachable vendor must not change where "I'm running low"
goes (ADR 005, and the module docstring in llm_extraction.py).

The entries are deliberately awkward. "Got any cancellations?" is a booking
that contains the word cancel, "Did I book something?" is a lookup that
contains the word book, and "How late can I cancel?" is a question about
policy that contains a request to cancel. Each one of those is a real caller
being misrouted into destroying an appointment, so each one is written down.
"""

from __future__ import annotations

import pytest
from tests.conftest import SEED_NOW

from app.agents.extraction import ExtractionContext, RuleBasedExtractor
from app.agents.factory import build_runtime
from app.agents.intents import Intent
from app.agents.orchestrator import FALLBACK_MESSAGE, Orchestrator
from app.agents.state import SessionState
from app.config.clinic import FAQ_TOPIC_ALIASES
from app.config.settings import Settings
from app.ehr.base import EHRProvider
from app.safety.triage import HUMAN_MESSAGE, check_human_requested


def route(utterance: str) -> Intent:
    return RuleBasedExtractor().extract(utterance, ExtractionContext()).intent


def topic(utterance: str) -> str | None:
    """The canonical topic, the way the FAQ workflow resolves it.

    The extractor returns whichever alias matched -- "saturday", "doctors" --
    and ``lookup_faq`` canonicalises. Asserting on the alias would pin the
    tests to the spelling rather than the answer.
    """
    matched = RuleBasedExtractor().extract(utterance, ExtractionContext()).faq_topic
    return None if matched is None else FAQ_TOPIC_ALIASES.get(matched, matched)


BOOKING = (
    "I need to see someone",
    "Can I get in this week?",
    "Do you have anything Tuesday?",
    "I want to come in about my knee",
    "When's your next opening with Dr. Patel?",
    "I need my diabetes check-up",
    "Set me up for a follow-up",
    "Got any cancellations?",
    "I'd like to be seen before Friday if possible",
    "My prescription check is due",
)

LOOKUP = (
    "When am I due in?",
    "Did I book something?",
    "What time is my thing on Thursday?",
    "Am I on the list for tomorrow?",
    "What appointments do I have?",
    "Have I got anything booked?",
)

CANCEL = (
    "I can't make it",
    "Something's come up",
    "Take me off for Tuesday",
    "I won't be there",
)

RESCHEDULE = (
    "Move my appointment",
    "Can we push it back a week?",
    "Scrap Thursday and give me Friday",
    "I need to change the day",
)

MEDICATION = (
    "What am I meant to be taking?",
    "Remind me what's on my prescription",
    "How many of the white ones a day?",
    "Is it one or two tablets?",
    "What does the label say for my blood pressure one?",
    "The asthma one, how often?",
    "My puffer",
    "I've forgotten the dose",
    "Do I take it with food?",
    "The sugar tablets",
)

REFILL = (
    "I'm running low",
    "I've nearly run out",
    "Can you send more to the pharmacy?",
    "I need another month of it",
    "My repeat is due",
    "Can you renew my script?",
    "I've got two left",
)

PERSONAL_COVERAGE = (
    "Am I covered?",
    "Is my insurance still on file?",
    "Did my plan go through?",
    "I changed jobs, is my new card on there?",
    "What plan have you got down for me?",
)

ACCEPTED_PLANS = (
    "Do you take Aetna?",
    "Are you in network?",
    "What insurers do you deal with?",
)

MONEY = (
    "How much is a visit?",
    "What's my copay?",
    "Will I be charged?",
    "Is this covered or am I paying?",
)

CLINIC_TOPICS = (
    ("Are you open Saturday?", "hours"),
    ("What time do you shut?", "hours"),
    ("Where are you?", "location"),
    ("Is there parking?", "parking"),
    ("How early should I turn up?", "arrival_time"),
    ("What do I need to bring?", "what_to_bring"),
    ("I'm a new patient, what happens?", "new_patient_process"),
    ("Who are your doctors?", "providers"),
    ("What's the number for the front desk?", "phone"),
    ("How late can I cancel?", "cancellation_policy"),
)

CLINIC_FACTS = tuple(utterance for utterance, _topic in CLINIC_TOPICS)

NO_INTENT = (
    "Hello?",
    "Hi",
    "Yeah, hi, um",
    "Is this the doctor's?",
    "Sorry, what?",
    "Hang on",
    "Can you repeat that?",
    "What can you do?",
)

HUMAN = (
    "Let me talk to a person",
    "Can I speak to reception?",
    "Is there someone there?",
    "I'd rather not do this with a robot",
    "Put me through",
)


class TestBooking:
    @pytest.mark.parametrize("utterance", BOOKING)
    def test_it_books(self, utterance: str) -> None:
        assert route(utterance) is Intent.BOOK_APPOINTMENT


class TestTheAppointmentVerbs:
    """Lookup, cancel and reschedule share a vocabulary and a victim.

    Confusing any two of them acts on a real booking: a lookup read as a cancel
    destroys an appointment the caller only wanted the time of.
    """

    @pytest.mark.parametrize("utterance", LOOKUP)
    def test_it_looks_up(self, utterance: str) -> None:
        assert route(utterance) is Intent.LOOKUP_APPOINTMENT

    @pytest.mark.parametrize("utterance", CANCEL)
    def test_it_cancels(self, utterance: str) -> None:
        assert route(utterance) is Intent.CANCEL_APPOINTMENT

    @pytest.mark.parametrize("utterance", RESCHEDULE)
    def test_it_reschedules(self, utterance: str) -> None:
        assert route(utterance) is Intent.RESCHEDULE_APPOINTMENT


class TestMedications:
    @pytest.mark.parametrize("utterance", MEDICATION)
    def test_it_reads_the_prescription(self, utterance: str) -> None:
        assert route(utterance) is Intent.MEDICATION_LOOKUP

    @pytest.mark.parametrize("utterance", REFILL)
    def test_running_low_is_a_refill_not_a_dosage_question(self, utterance: str) -> None:
        """ "How much do I take" and "I'm nearly out" are different requests."""
        assert route(utterance) is Intent.REFILL_REQUEST


class TestInsuranceAndMoney:
    """Three questions that share every word except the possessive."""

    @pytest.mark.parametrize("utterance", PERSONAL_COVERAGE)
    def test_their_own_cover_is_a_record_read(self, utterance: str) -> None:
        assert route(utterance) is Intent.COVERAGE_LOOKUP

    @pytest.mark.parametrize("utterance", ACCEPTED_PLANS)
    def test_which_plans_we_accept_is_public(self, utterance: str) -> None:
        assert route(utterance) is Intent.CLINIC_FAQ
        assert topic(utterance) == "insurance"

    @pytest.mark.parametrize("utterance", MONEY)
    def test_money_goes_to_the_front_desk(self, utterance: str) -> None:
        assert route(utterance) is Intent.CLINIC_FAQ
        assert topic(utterance) == "billing"


class TestClinicFacts:
    @pytest.mark.parametrize(("utterance", "expected"), CLINIC_TOPICS)
    def test_the_topic_is_recognised(self, utterance: str, expected: str) -> None:
        assert route(utterance) is Intent.CLINIC_FAQ
        assert topic(utterance) == expected


class TestNothingWasAsked:
    """The most common opening line, now that the agent greets first.

    People answer a greeting with a greeting. None of these carry a request,
    and inventing one for them is how a caller who said "hello?" ends up in a
    booking flow. UNKNOWN is the correct answer, and the orchestrator turns it
    into the list of things the agent can do.
    """

    @pytest.mark.parametrize("utterance", NO_INTENT)
    def test_no_intent_is_invented(self, utterance: str) -> None:
        assert route(utterance) is Intent.UNKNOWN


class TestAskingForAPerson:
    """Handled before extraction: a request for a human is never negotiated."""

    @pytest.mark.parametrize("utterance", HUMAN)
    def test_it_reaches_a_human(self, utterance: str) -> None:
        assert check_human_requested(utterance) is not None


class TestNothingDeadEnds:
    """Routing correctly is not the same as being answered.

    An intent that reaches a workflow which then has nothing to say is the
    same experience as not being understood, so every phrasing in this file is
    also run through the real orchestrator: whatever comes back must not be
    the capability menu, which is what the agent says when it has no idea.
    """

    @pytest.fixture
    def orchestrator(self, memory_ehr: EHRProvider) -> Orchestrator:
        return build_runtime(
            ehr=memory_ehr, settings=Settings(_env_file=None, app_env="test")
        ).orchestrator

    @pytest.mark.parametrize(
        "utterance",
        BOOKING
        + LOOKUP
        + CANCEL
        + RESCHEDULE
        + MEDICATION
        + REFILL
        + PERSONAL_COVERAGE
        + CLINIC_FACTS
        + MONEY
        + ACCEPTED_PLANS,
    )
    async def test_the_agent_has_something_to_say(
        self, orchestrator: Orchestrator, utterance: str
    ) -> None:
        session = SessionState(session_id="sess-phrase", created_at=SEED_NOW)
        result = await orchestrator.handle_turn(session, utterance, now=SEED_NOW)
        assert result.message != FALLBACK_MESSAGE

    @pytest.mark.parametrize("utterance", NO_INTENT)
    async def test_an_opening_pleasantry_gets_the_menu(
        self, orchestrator: Orchestrator, utterance: str
    ) -> None:
        """The right answer to "hello?" is what the agent can do.

        The most common first utterance now that the agent greets first --
        people answer a greeting with a greeting -- so it matters that this
        stays a real answer rather than becoming a guess at a workflow.
        """
        session = SessionState(session_id="sess-phrase", created_at=SEED_NOW)
        result = await orchestrator.handle_turn(session, utterance, now=SEED_NOW)
        assert result.message == FALLBACK_MESSAGE

    @pytest.mark.parametrize("utterance", HUMAN)
    async def test_a_request_for_a_person_is_honoured(
        self, orchestrator: Orchestrator, utterance: str
    ) -> None:
        session = SessionState(session_id="sess-phrase", created_at=SEED_NOW)
        result = await orchestrator.handle_turn(session, utterance, now=SEED_NOW)
        assert result.message == HUMAN_MESSAGE


REJECTING_THE_TIMES = (
    "None of those work",
    "None of them suit me",
    "Have you got anything else?",
    "What else have you got?",
    "Something else please",
    "Have you got another day?",
    "Those don't work for me",
    "That's too early",
    "Any other times?",
)


class TestTurningDownEveryTime:
    """ "Not one of those" said nine ways, all of which mean search again.

    Read only while times are on the table, which is what makes the looser
    phrasings safe: "anything else" is a request for other times when it
    follows a list of them. A rejection the agent does not hear is a caller
    being read the same three slots a second time.
    """

    @pytest.mark.parametrize("utterance", REJECTING_THE_TIMES)
    def test_it_reads_as_a_rejection(self, utterance: str) -> None:
        extracted = RuleBasedExtractor().extract(utterance, ExtractionContext())
        assert extracted.none_suitable

    @pytest.mark.parametrize(
        "utterance",
        ["The second one", "Tuesday please", "Yes, book it", "The first one, please"],
    )
    def test_choosing_one_is_not_rejecting_them_all(self, utterance: str) -> None:
        extracted = RuleBasedExtractor().extract(utterance, ExtractionContext())
        assert not extracted.none_suitable
