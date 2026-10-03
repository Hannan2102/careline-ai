"""Understanding what the caller meant, rather than what they happened to say.

The rules in ``extraction.py`` match phrases. That works until someone phrases
it differently, and people always do: "what does my prescription say", "how do
I use my inhaler", "what am I meant to be taking" and "remind me about the
asthma one" are one request in four costumes, and every costume the table does
not hold is met with a menu. Each such miss was fixed by hand, one phrase at a
time, which is not a strategy -- it is a queue.

So the model classifies, and nothing else. It sees the caller's words, the
question the agent last asked, and the list of intents. It returns which intent
and which entities it heard. It does not decide what is allowed, does not read
the record, does not compose the reply, and never sees patient data -- those
belong to deterministic Python, which is the whole architecture (README, ADR
005).

Three properties make that safe to rely on:

* **The rules are the floor, not the ceiling.** They run first, always. The
  model may only fill a gap or overrule an intent the rules were unsure of; a
  timeout, a rate limit, a malformed reply or mock mode all land back on the
  rules, and the caller notices nothing.
* **Values stay deterministic.** Dates, spoken digits and slot ordinals are
  parsed by code that has been hardened against real calls. The model is better
  at knowing a date of birth was offered; the rules are better at knowing which
  date -- so the model is no longer asked for the date at all, only whether one
  was given (``dob_offered``). Where both speak, the rules win. A name or a
  spelling the model fills in is never acted on unseen: the identity steps
  spell it back to the caller before anything is checked (ADR 010).
* **Safety runs earlier.** The classifier cannot reach a clinical question:
  those are refused before extraction happens at all.
"""

from __future__ import annotations

import asyncio
import json
import re

from pydantic import BaseModel, ConfigDict, ValidationError

from app.agents.extraction import ExtractedTurn, ExtractionContext, TurnExtractor
from app.agents.intents import Intent
from app.ai.providers.base import ChatMessage, LLMProvider, LLMRequest, ProviderUnavailableError
from app.observability.logging import get_logger
from app.workflows.base import AwaitedInput

logger = get_logger(__name__)

#: Long enough for the JSON object below, short enough that a model which
#: starts narrating is cut off rather than paid for.
#:
#: Raised from 200 when three fields were added: models echo every key in the
#: schema with a null rather than omitting it, so the reply grew past the cap,
#: arrived without its closing brace, and was discarded whole. The symptom was
#: not an error -- it was the classifier quietly getting worse at everything,
#: which is the hardest kind of failure to notice and took a raw dump of the
#: response to see. Any headroom is cheaper than that.
MAX_OUTPUT_TOKENS = 400

#: Lists read to a caller are three or four long; they have to be, because
#: nobody holds ten options in their head over the phone.
MAX_PLAUSIBLE_POSITION = 5

#: Classification is not a creative task.
TEMPERATURE = 0.0

#: A caller is waiting. Past this the rules answer instead -- a slightly blunter
#: understanding now beats a better one after a silence.
TIMEOUT_SECONDS = 3.0

#: Kept deliberately terse. It is re-sent on every classified turn, and Groq's
#: free tier meters tokens per minute -- a verbose prompt is not just slower to
#: read, it is fewer turns per minute before the vendor starts refusing. The
#: earlier draft ran 556 tokens and throttled after about thirteen
#: classifications; this one is roughly half that.
#:
#: Only the distinctions the rules genuinely cannot make are spelled out. The
#: rest the model already knows, and restating it buys nothing.
SYSTEM_PROMPT = f"""\
Classify what a caller to a medical clinic wants. Reply with ONLY a JSON object:
{{"intent":..,"confidence":0-1,"given_name":..,"family_name":..,"spelled":..,
"dob_offered":..,"patient_status":..,"medication_name":..,"faq_topic":..,"confirm":..,"list_all":..,
"none_suitable":..,"ordinal":..,"out_of_scope":..,"changes_subject":..}}
Use null for anything not said. Never invent a name or drug.
spelled: letters they spelled out, joined, e.g. "SMYTH".
dob_offered: true if they gave a date of birth. Do not repeat the date.
patient_status: "existing" if already a patient here, "new" if not.
ordinal: which of a numbered list they chose, counting from 1. Any way of
picking one counts: by position, by day, by time of day, by clinician.
none_suitable: they turned down everything offered, however they said it.
confirm: true for yes, false for no.
out_of_scope: a real request this line cannot serve -- a complaint, test
results, a referral chased, anything for a clinician to answer.
changes_subject: true if they want something other than an answer to that
question, including something added to it ("while I'm on", "can you also",
"before I go"). False if they are answering it, however clumsily or partly.

intent: {", ".join(i.value for i in Intent)}

faq_topic (clinic_faq only): hours location phone parking arrival_time
cancellation_policy what_to_bring new_patient_process insurance billing providers

Whose thing it is decides the intent:
- their own insurance/plan -> coverage_lookup; which insurers we accept -> clinic_faq
- price of a visit -> clinic_faq + billing; dose of their drug -> medication_lookup
- more of a drug -> refill_request; what a drug says -> medication_lookup

If they are answering the agent's last question, classify by what it answered.
Unsure -> unknown, low confidence. A wrong intent is worse than none."""


#: What the agent had just asked, in words the model can use. Named only --
#: never the options themselves, which for appointments and prescriptions are
#: the patient's record.
_QUESTION_ASKED: dict[AwaitedInput, str] = {
    AwaitedInput.PATIENT_STATUS: "whether they are an existing patient or new to the clinic",
    AwaitedInput.NAME: "for the caller's first and last name",
    AwaitedInput.NAME_CONFIRMATION: "whether the spelling of their name it read back is right",
    AwaitedInput.NAME_SPELLING: "the caller to spell their name",
    AwaitedInput.DATE_OF_BIRTH: "for the caller's date of birth",
    AwaitedInput.DOB_CONFIRMATION: "whether the date of birth it read back is right",
    AwaitedInput.SECOND_FACTOR: "for the last four digits of their phone number",
    AwaitedInput.REASON: "what the appointment is for",
    AwaitedInput.SLOT_CHOICE: "which of the appointment times it offered they would like",
    AwaitedInput.MEDICATION_CHOICE: "which of their medications they meant",
    AwaitedInput.CONFIRMATION: "whether to go ahead",
}


def _rules_answered(awaiting: AwaitedInput, baseline: ExtractedTurn) -> bool:
    """Whether the rules already got what the outstanding question needed.

    Per question, because "answered" means something different for each: a
    name or a date of birth for identity, a choice or a refusal for a list of
    times. Anything not listed is treated as answered, so a new kind of
    question cannot silently start a model call on every turn.
    """
    match awaiting:
        case AwaitedInput.PATIENT_STATUS:
            return baseline.patient_status is not None or baseline.full_name is not None
        case AwaitedInput.NAME:
            return baseline.full_name is not None
        case AwaitedInput.NAME_CONFIRMATION:
            return (
                baseline.confirm is not None
                or baseline.spelled is not None
                or baseline.full_name is not None
            )
        case AwaitedInput.NAME_SPELLING:
            return baseline.spelled is not None
        case AwaitedInput.DATE_OF_BIRTH:
            # Nothing the model says can supply the date itself, so a miss here
            # is the parser's to explain with its hint, not the model's to fill.
            return True
        case AwaitedInput.DOB_CONFIRMATION:
            return baseline.confirm is not None or baseline.date_of_birth is not None
        case AwaitedInput.SECOND_FACTOR:
            return baseline.second_factor_value is not None
        case AwaitedInput.SLOT_CHOICE:
            return (
                baseline.ordinal is not None
                or baseline.none_suitable
                or baseline.confirm is not None
            )
        case AwaitedInput.MEDICATION_CHOICE:
            return (
                baseline.medication_name is not None
                or baseline.ordinal is not None
                or baseline.list_all
                or baseline.confirm is not None
            )
        case AwaitedInput.CONFIRMATION:
            return baseline.confirm is not None
        case _:
            return True


class _Classification(BaseModel):
    """The model's reply, before it is believed.

    Every field optional: a model that omits one is normal, and a model that
    invents a key is ignored. Validation failure is not an error path worth
    escalating -- it falls back to the rules like any other miss.
    """

    model_config = ConfigDict(extra="ignore")

    intent: str | None = None
    confidence: float = 0.0
    given_name: str | None = None
    family_name: str | None = None
    #: Older replies, and models that ignore the schema, still send this.
    full_name: str | None = None
    spelled: str | None = None
    dob_offered: bool | None = None
    patient_status: str | None = None
    medication_name: str | None = None
    faq_topic: str | None = None
    confirm: bool | None = None
    ordinal: int | None = None
    # Optional, not defaulted-false: a model asked for a key it has no opinion
    # about returns `null`, and a schema that refuses null threw away the whole
    # classification over a field nobody had asked about. Measured against Groq
    # this rejected every single reply.
    list_all: bool | None = None
    none_suitable: bool | None = None
    out_of_scope: bool | None = None
    changes_subject: bool | None = None

    def position(self) -> int | None:
        """The chosen position, if it could be one.

        Anything outside a short list is a number the model found somewhere in
        the sentence rather than a choice -- a year, a house number, a dose.
        Dropping it costs a re-ask; keeping it acts on the wrong option.
        """
        if self.ordinal is None or not 1 <= self.ordinal <= MAX_PLAUSIBLE_POSITION:
            return None
        return self.ordinal

    def name(self) -> str | None:
        """The name it heard, if it is shaped like one.

        Letters, apostrophes, hyphens and spaces only, one to four words. A
        model that answers "the caller did not say" in this field has not
        given a name.
        """
        joined = " ".join(p for p in (self.given_name, self.family_name) if p) or self.full_name
        if not joined:
            return None
        words = joined.split()
        if not 1 <= len(words) <= 4 or not all(_NAME_WORD.fullmatch(w) for w in words):
            return None
        return " ".join(w[0].upper() + w[1:] for w in words)

    def letters(self) -> str | None:
        """What it heard spelled, upper-case, if there were letters in it."""
        if not self.spelled:
            return None
        cleaned = re.sub(r"[^A-Z' -]", "", self.spelled.upper()).replace("-", "")
        cleaned = " ".join(cleaned.split())
        return cleaned if sum(c.isalpha() for c in cleaned) >= 2 else None


_NAME_WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")

#: The identity questions whose answer can be a name.
_NAME_QUESTIONS = frozenset(
    {AwaitedInput.NAME, AwaitedInput.NAME_CONFIRMATION, AwaitedInput.NAME_SPELLING}
)


class LLMExtractor:
    """Classifies with a model, falling back to the rules whenever it cannot."""

    def __init__(
        self,
        llm: LLMProvider,
        rules: TurnExtractor,
        timeout: float = TIMEOUT_SECONDS,
    ) -> None:
        self.llm = llm
        self.rules = rules
        self.timeout = timeout

    def extract(self, utterance: str, context: ExtractionContext) -> ExtractedTurn:
        """The rules alone. Satisfies the synchronous half of the Protocol."""
        return self.rules.extract(utterance, context)

    async def aextract(self, utterance: str, context: ExtractionContext) -> ExtractedTurn:
        baseline = self.rules.extract(utterance, context)

        if not self._worth_asking(utterance, context, baseline):
            return baseline

        classification = await self._classify(utterance, context, baseline)
        if classification is None:
            return baseline
        return self._merge(baseline, classification, context)

    # ------------------------------------------------------------ when to ask
    @staticmethod
    def _worth_asking(utterance: str, context: ExtractionContext, baseline: ExtractedTurn) -> bool:
        """Whether a model call would tell us anything we do not already have.

        Mid-conversation the test is whether the rules answered the question
        the agent asked. When they did -- "the second one", "yes", a date of
        birth read off a card -- there is nothing to add, and a round trip
        would put 400 ms into the most latency-sensitive turns in the call for
        a result already in hand.

        When they did not, the turn is otherwise lost: the agent is about to
        ask the same question a second time, which is the moment a caller
        decides this was a waste of their afternoon. Latency has stopped
        competing with a good answer, so the model gets its go. That is the
        same "rules are the floor" bargain the rest of this module makes,
        extended to the half of the conversation it used to sit out.
        """
        if len(utterance.strip()) <= 2:
            return False
        if context.awaiting is None:
            return True
        return not _rules_answered(context.awaiting, baseline)

    # -------------------------------------------------------------- the call
    async def _classify(
        self, utterance: str, context: ExtractionContext, baseline: ExtractedTurn
    ) -> _Classification | None:
        request = LLMRequest(
            messages=[
                ChatMessage(role="system", content=SYSTEM_PROMPT),
                ChatMessage(role="user", content=self._question_and_answer(utterance, context)),
            ],
            max_output_tokens=MAX_OUTPUT_TOKENS,
            temperature=TEMPERATURE,
        )
        try:
            response = await asyncio.wait_for(self.llm.generate(request), timeout=self.timeout)
        except TimeoutError:
            logger.warning("llm_extraction_timed_out", seconds=self.timeout)
            return None
        except ProviderUnavailableError as exc:
            logger.warning("llm_extraction_unavailable", error=str(exc))
            return None
        except Exception as exc:  # a classifier must never end a call
            logger.error("llm_extraction_failed", error=str(exc))
            return None

        return self._parse(response.text)

    @staticmethod
    def _question_and_answer(utterance: str, context: ExtractionContext) -> str:
        """The caller's words, and what they were answering.

        A bare "the one in the morning" is unclassifiable alone and obvious
        against the question it answers.

        Appointment times the clinic has free go with it, because that is what
        makes "the one in the morning", "whichever is soonest" and "the one
        with Dr Chen" answerable at all -- and because an empty slot in a
        diary is not information about a patient. Nothing else does: which
        appointments this caller has and what is on their prescription are
        their record, and the record does not leave this process (ADR 005).
        The question is named; its answers are not.
        """
        asked = _QUESTION_ASKED.get(context.awaiting) if context.awaiting else None
        if asked is None:
            return utterance
        lines = [f"The agent asked {asked}."]
        if context.awaiting is AwaitedInput.SLOT_CHOICE and context.offers:
            listed = "; ".join(f"{offer.index}) {offer.label}" for offer in context.offers)
            lines.append(f"The times, in order: {listed}.")
        lines.append(f'The caller replied: "{utterance}"')
        return " ".join(lines)

    @staticmethod
    def _parse(text: str) -> _Classification | None:
        """Read the JSON object out of whatever came back.

        Models wrap JSON in prose and in code fences however firmly they are
        asked not to, so the object is located rather than assumed.
        """
        if not text:
            return None
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            # Nearly always a reply that ran out of tokens mid-object. Worth a
            # line in the log: on the way past, it looks like a model that has
            # got worse rather than one that was cut off.
            logger.warning("llm_extraction_incomplete", reply=text[-60:])
            return None
        try:
            return _Classification.model_validate(json.loads(text[start : end + 1]))
        except (ValueError, ValidationError) as exc:
            logger.warning("llm_extraction_unparseable", error=str(exc))
            return None

    # ------------------------------------------------------------- the merge
    @staticmethod
    def _merge(
        baseline: ExtractedTurn, seen: _Classification, context: ExtractionContext | None = None
    ) -> ExtractedTurn:
        """Rules for values, model for meaning.

        The split is deliberate and is the point of the whole module. Which
        date was said is a parsing problem, solved by code that has been
        hardened against real calls and is worth trusting. *That* a date of
        birth was being offered at all, or that "remind me about the asthma
        one" is a prescription question, is a comprehension problem, and the
        model is better at it than any table of phrases.

        So the model may overrule an intent the rules did not find, and may
        fill an entity the rules missed -- but never overwrite one they found.

        A name or spelling from the model is taken only when the agent asked
        for one. Elsewhere a name in a sentence is somebody's mother, and the
        model's guess at one used to be "John Doe" (PROJECT_STATUS, Phase 12).
        """
        awaiting = context.awaiting if context else None
        asked_for_a_name = awaiting in _NAME_QUESTIONS
        intent = baseline.intent
        confidence = baseline.confidence
        if baseline.intent is Intent.UNKNOWN and seen.intent:
            try:
                candidate = Intent(seen.intent)
            except ValueError:
                logger.warning("llm_extraction_unknown_intent", intent=seen.intent)
                candidate = Intent.UNKNOWN
            if candidate is not Intent.UNKNOWN:
                intent = candidate
                confidence = min(seen.confidence, 0.9)

        return ExtractedTurn(
            intent=intent,
            confidence=confidence,
            full_name=baseline.full_name or (seen.name() if asked_for_a_name else None),
            # Never the model's: it says *that* a date was given, the parser
            # says which (dob_parser.py).
            date_of_birth=baseline.date_of_birth,
            dob_candidates=baseline.dob_candidates,
            dob_offered=bool(seen.dob_offered),
            patient_status=baseline.patient_status
            or (
                seen.patient_status
                if awaiting is AwaitedInput.PATIENT_STATUS
                and seen.patient_status in ("existing", "new")
                else None
            ),
            spelled=baseline.spelled
            or (
                seen.letters()
                if awaiting in (AwaitedInput.NAME_CONFIRMATION, AwaitedInput.NAME_SPELLING)
                else None
            ),
            second_factor_value=baseline.second_factor_value,
            reason=baseline.reason,
            practitioner_name=baseline.practitioner_name,
            medication_name=baseline.medication_name or seen.medication_name,
            faq_topic=baseline.faq_topic
            or (seen.faq_topic if intent is Intent.CLINIC_FAQ else None),
            # A position the rules could not find. They resolve "the second
            # one" and a named day against the list actually offered, which is
            # the reliable half of this and stays theirs; what the model adds
            # is the half a table cannot hold -- "the one after that", "the
            # earliest you said". Only ever when the rules found nothing, and
            # only a plausible position: a hallucinated 7 against three
            # options books the wrong thing rather than asking.
            ordinal=baseline.ordinal if baseline.ordinal is not None else seen.position(),
            confirm=baseline.confirm if baseline.confirm is not None else seen.confirm,
            none_suitable=baseline.none_suitable or bool(seen.none_suitable),
            out_of_scope=bool(seen.out_of_scope),
            # Only the model may say this, and only about an intent it also
            # supplied: it is the one judgement here that abandons work in
            # progress, and the rules cannot tell "actually, do my refill
            # instead" from a clumsy answer to the question just asked.
            changes_subject=bool(seen.changes_subject) and intent is not Intent.UNKNOWN,
            # Only when nothing was named. "Tell me how much metformin I should
            # take" is a question about one drug, and a model that also sets
            # list_all turns it into a recital of the whole record -- which is
            # what a live caller got, twice, having asked about metformin by
            # name both times.
            list_all=baseline.list_all
            or (bool(seen.list_all) and not (baseline.medication_name or seen.medication_name)),
            entities=baseline.entities,
        )
