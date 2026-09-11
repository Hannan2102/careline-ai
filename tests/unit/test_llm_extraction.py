"""Classifying with a model, without depending on one.

No test here reaches a vendor. What is tested is the contract around the model:
that the rules are the floor, that every way a model can fail lands back on
them, and that a classification is never allowed to overwrite a value the rules
parsed for real.

The behaviour that needs a live model -- whether it actually understands
"remind me what I'm meant to be taking" -- is not something a test can assert
without paying for it on every run. That was measured against Groq by hand and
recorded in the commit.
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from app.agents.extraction import ExtractionContext, RuleBasedExtractor
from app.agents.intents import Intent
from app.agents.llm_extraction import LLMExtractor
from app.ai.providers.base import LLMRequest, LLMResponse, LLMUsage, ProviderUnavailableError
from app.workflows.base import AwaitedInput


class FakeLLM:
    """Returns whatever it was handed, and counts how often it was asked."""

    name = "fake"

    def __init__(self, reply: str = "", error: Exception | None = None, delay: float = 0.0) -> None:
        self.reply = reply
        self.error = error
        self.delay = delay
        self.calls = 0
        #: Everything sent, so tests can assert on what the model was told --
        #: and on what it was not.
        self.prompts: list[str] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        self.prompts.append("\n".join(message.content for message in request.messages))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return LLMResponse(text=self.reply, usage=LLMUsage())

    async def tool_call(self, request):  # type: ignore[no-untyped-def] - unused here
        raise NotImplementedError


def extractor(llm: FakeLLM, timeout: float = 3.0) -> LLMExtractor:
    return LLMExtractor(llm, RuleBasedExtractor(), timeout=timeout)


class TestTheModelAddsUnderstanding:
    async def test_an_intent_the_rules_missed_is_rescued(self) -> None:
        llm = FakeLLM('{"intent": "medication_lookup", "confidence": 0.9}')
        rules_only = RuleBasedExtractor().extract(
            "what did they put me on after my last visit", ExtractionContext()
        )
        assert rules_only.intent is Intent.UNKNOWN

        result = await extractor(llm).aextract(
            "what did they put me on after my last visit", ExtractionContext()
        )
        assert result.intent is Intent.MEDICATION_LOOKUP

    async def test_json_wrapped_in_prose_is_still_read(self) -> None:
        """Models add commentary however firmly they are told not to."""
        llm = FakeLLM(
            'Sure! ```json\n{"intent": "clinic_faq", "confidence": 0.8}\n``` Hope that helps.'
        )
        result = await extractor(llm).aextract("what time do you shut", ExtractionContext())
        assert result.intent is Intent.CLINIC_FAQ

    async def test_an_entity_the_rules_missed_is_filled_in(self) -> None:
        llm = FakeLLM('{"intent": "medication_lookup", "medication_name": "inhaler"}')
        result = await extractor(llm).aextract("the blue one for my chest", ExtractionContext())
        assert result.medication_name == "inhaler"


class TestTheRulesAreTheFloor:
    """Every way a model can fail has to be survivable."""

    @pytest.mark.parametrize(
        ("name", "llm"),
        [
            ("unreachable", FakeLLM(error=ProviderUnavailableError("429 rate limited"))),
            ("crashed", FakeLLM(error=RuntimeError("boom"))),
            ("empty", FakeLLM(reply="")),
            ("not json", FakeLLM(reply="I think they want an appointment")),
            ("truncated json", FakeLLM(reply='{"intent": "book_app')),
            ("wrong shape", FakeLLM(reply='["book_appointment"]')),
            ("invented intent", FakeLLM(reply='{"intent": "order_a_pizza"}')),
        ],
    )
    async def test_a_failing_model_leaves_the_rules_answer(self, name: str, llm: FakeLLM) -> None:
        result = await extractor(llm).aextract("I need to cancel", ExtractionContext())
        assert result.intent is Intent.CANCEL_APPOINTMENT, f"{name} lost the rules answer"

    async def test_a_slow_model_is_abandoned(self) -> None:
        """A caller is on the phone. The rules answer rather than the line going quiet."""
        llm = FakeLLM('{"intent": "book_appointment"}', delay=0.5)
        result = await extractor(llm, timeout=0.05).aextract(
            "I need to cancel", ExtractionContext()
        )
        assert result.intent is Intent.CANCEL_APPOINTMENT

    async def test_a_confident_rule_is_not_overruled(self) -> None:
        """The model only fills a gap; it does not get a second opinion.

        A model that decides "I need to cancel my appointment" is a booking
        would otherwise cancel nothing and book something.
        """
        llm = FakeLLM('{"intent": "book_appointment", "confidence": 0.99}')
        result = await extractor(llm).aextract(
            "I need to cancel my appointment", ExtractionContext()
        )
        assert result.intent is Intent.CANCEL_APPOINTMENT


class TestValuesStayDeterministic:
    async def test_a_parsed_date_is_never_replaced(self) -> None:
        """The rules parse dates. The model does not get to revise one."""
        llm = FakeLLM('{"intent": "unknown", "date_of_birth": "1999-01-01"}')
        result = await extractor(llm).aextract(
            "I was born on the fifteenth of February nineteen eighty five",
            ExtractionContext(awaiting=None),
        )
        assert result.date_of_birth == date(1985, 2, 15)

    async def test_an_unreadable_date_costs_only_the_date(self) -> None:
        """One bad field must not discard the whole classification.

        Measured against Groq: the model echoed the prompt's own "YYYY-MM-DD"
        placeholder, strict validation rejected the entire object, and
        classification accuracy fell from 9/10 to 1/10 -- over a field that had
        nothing to do with the question.
        """
        llm = FakeLLM('{"intent": "clinic_faq", "confidence": 0.9, "date_of_birth": "YYYY-MM-DD"}')
        result = await extractor(llm).aextract("what time do you shut", ExtractionContext())
        assert result.intent is Intent.CLINIC_FAQ
        assert result.date_of_birth is None

    @pytest.mark.parametrize("absent", ["null", "none", "", "   "])
    async def test_the_word_null_is_not_a_date(self, absent: str) -> None:
        llm = FakeLLM(f'{{"intent": "clinic_faq", "date_of_birth": "{absent}"}}')
        result = await extractor(llm).aextract("what time do you shut", ExtractionContext())
        assert result.intent is Intent.CLINIC_FAQ
        assert result.date_of_birth is None

    async def test_a_null_boolean_is_not_a_failure(self) -> None:
        """Also measured live: it rejected every reply the model sent."""
        llm = FakeLLM('{"intent": "clinic_faq", "list_all": null, "none_suitable": null}')
        result = await extractor(llm).aextract("what time do you shut", ExtractionContext())
        assert result.intent is Intent.CLINIC_FAQ
        assert result.list_all is False


class TestWhenTheModelIsAskedAtAll:
    """Latency and vendor quota are both spent per call, so calls are earned."""

    async def test_mid_workflow_turns_do_not_call_the_model(self) -> None:
        """The agent just asked for a date of birth; the turn is the answer.

        Classifying it would add a round trip to every identity step to learn
        something the outstanding question already settled.
        """
        llm = FakeLLM('{"intent": "book_appointment"}')
        await extractor(llm).aextract(
            "fifteenth of February nineteen eighty five",
            ExtractionContext(awaiting=AwaitedInput.IDENTITY),
        )
        assert llm.calls == 0

    async def test_a_grunt_does_not_call_the_model(self) -> None:
        llm = FakeLLM('{"intent": "book_appointment"}')
        await extractor(llm).aextract("uh", ExtractionContext())
        assert llm.calls == 0

    async def test_an_open_question_does_call_the_model(self) -> None:
        llm = FakeLLM('{"intent": "clinic_faq"}')
        await extractor(llm).aextract("where do I park", ExtractionContext())
        assert llm.calls == 1


class TestListAllIsNotAnOverride:
    """Asking about one drug is not asking about all of them.

    Measured on a live call: the caller asked about metformin by name, the
    model returned medication_lookup with list_all also set, and the agent
    recited the whole record instead of answering. Twice.
    """

    async def test_a_named_drug_survives_the_model_asking_for_everything(self) -> None:
        llm = FakeLLM('{"intent": "medication_lookup", "list_all": true}')
        result = await extractor(llm).aextract(
            "tell me how much metformin I should take", ExtractionContext()
        )
        assert result.medication_name is not None
        assert result.list_all is False

    async def test_list_all_still_works_when_nothing_was_named(self) -> None:
        llm = FakeLLM('{"intent": "medication_lookup", "list_all": true}')
        result = await extractor(llm).aextract("what am I on at the moment", ExtractionContext())
        assert result.list_all is True


class TestTheModelIsAskedMidConversation:
    """The half of the conversation the model used to sit out.

    Every phrasing that broke on a live call this week was an answer to a
    question the agent had just asked -- "the Tuesday one", "have you got
    anything else", "for the first one, please" -- and the model was switched
    off for all of them. It is now asked, but only when the rules came up
    empty: when they have the answer there is nothing to add, and the turns
    where they have it are the ones a caller notices a delay on.
    """

    async def test_an_answer_the_rules_understood_costs_nothing(self) -> None:
        llm = FakeLLM('{"ordinal": 3}')
        result = await extractor(llm).aextract(
            "the second one", ExtractionContext(awaiting=AwaitedInput.SLOT_CHOICE)
        )
        assert llm.calls == 0
        assert result.ordinal == 2

    async def test_an_answer_they_missed_reaches_the_model(self) -> None:
        llm = FakeLLM('{"ordinal": 2}')
        result = await extractor(llm).aextract(
            "I'll take the one after that",
            ExtractionContext(awaiting=AwaitedInput.SLOT_CHOICE),
        )
        assert llm.calls == 1
        assert result.ordinal == 2

    async def test_a_date_of_birth_the_rules_parsed_costs_nothing(self) -> None:
        llm = FakeLLM('{"intent": "book_appointment"}')
        await extractor(llm).aextract(
            "fifteenth of February nineteen eighty five",
            ExtractionContext(awaiting=AwaitedInput.IDENTITY),
        )
        assert llm.calls == 0

    async def test_the_model_is_told_what_was_asked(self) -> None:
        """Without it, "the one in the morning" is not classifiable at all."""
        llm = FakeLLM('{"ordinal": 1}')
        await extractor(llm).aextract(
            "the one in the morning", ExtractionContext(awaiting=AwaitedInput.SLOT_CHOICE)
        )
        assert "which of the appointment times" in llm.prompts[-1]
        assert "the one in the morning" in llm.prompts[-1]

    async def test_it_is_never_told_what_the_options_were(self) -> None:
        """The options are the patient's record. They stay on this side.

        A list of their appointments or their prescriptions is exactly the
        data this module promises never to send (ADR 005), so the model is
        told which question was asked and never its answers.
        """
        llm = FakeLLM('{"medication_name": "the blue one"}')
        await extractor(llm).aextract(
            "the one for my chest",
            ExtractionContext(
                awaiting=AwaitedInput.MEDICATION_CHOICE,
                known_medications=("Metformin 500 mg", "Lisinopril 10 mg"),
            ),
        )
        assert "Metformin" not in llm.prompts[-1]
        assert "Lisinopril" not in llm.prompts[-1]


class TestAPositionIsNotJustANumber:
    @pytest.mark.parametrize("absurd", [0, 7, 99, 1985, -1])
    async def test_an_implausible_position_is_dropped(self, absurd: int) -> None:
        """A number found in the sentence is not a choice from a list of three.

        Acting on it books the wrong time; dropping it costs one re-ask.
        """
        llm = FakeLLM(f'{{"ordinal": {absurd}}}')
        result = await extractor(llm).aextract(
            "I'll have the one in nineteen eighty five",
            ExtractionContext(awaiting=AwaitedInput.SLOT_CHOICE),
        )
        assert result.ordinal is None

    async def test_the_rules_position_is_never_overwritten(self) -> None:
        llm = FakeLLM('{"ordinal": 3}')
        result = await extractor(llm).aextract("the first one, please", ExtractionContext())
        assert result.ordinal == 1


class TestOutOfScope:
    """Understood perfectly, and not something this line can do."""

    async def test_the_model_can_say_it_is_out_of_scope(self) -> None:
        llm = FakeLLM('{"intent": "unknown", "out_of_scope": true, "confidence": 0.9}')
        result = await extractor(llm).aextract(
            "can you send my notes to my solicitor", ExtractionContext()
        )
        assert result.out_of_scope

    async def test_the_rules_never_claim_it(self) -> None:
        """Knowing the limits of the system is not a phrase-table judgement."""
        assert not RuleBasedExtractor().extract("anything at all", ExtractionContext()).out_of_scope

    async def test_a_recognised_request_is_not_out_of_scope(self) -> None:
        llm = FakeLLM('{"intent": "book_appointment", "out_of_scope": true}')
        result = await extractor(llm).aextract("I need an appointment", ExtractionContext())
        assert result.intent is Intent.BOOK_APPOINTMENT
