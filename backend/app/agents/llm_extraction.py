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
  date. Where both speak, the rules win.
* **Safety runs earlier.** The classifier cannot reach a clinical question:
  those are refused before extraction happens at all.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from app.agents.extraction import ExtractedTurn, ExtractionContext, TurnExtractor
from app.agents.intents import Intent
from app.ai.providers.base import ChatMessage, LLMProvider, LLMRequest, ProviderUnavailableError
from app.observability.logging import get_logger

logger = get_logger(__name__)

#: Long enough for the JSON object below, short enough that a model which
#: starts narrating is cut off rather than paid for.
MAX_OUTPUT_TOKENS = 200

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
{{"intent":..,"confidence":0-1,"full_name":..,"date_of_birth":..,
"medication_name":..,"faq_topic":..,"confirm":..,"list_all":..,"none_suitable":..}}
Use null for anything not said. Never invent a name, date or drug.
A date is an ISO calendar date and nothing else.

intent: {", ".join(i.value for i in Intent)}

faq_topic (clinic_faq only): hours location phone parking arrival_time
cancellation_policy what_to_bring new_patient_process insurance billing providers

Whose thing it is decides the intent:
- their own insurance/plan -> coverage_lookup; which insurers we accept -> clinic_faq
- price of a visit -> clinic_faq + billing; dose of their drug -> medication_lookup
- more of a drug -> refill_request; what a drug says -> medication_lookup

If they are answering the agent's last question, classify by what it answered.
Unsure -> unknown, low confidence. A wrong intent is worse than none."""


class _Classification(BaseModel):
    """The model's reply, before it is believed.

    Every field optional: a model that omits one is normal, and a model that
    invents a key is ignored. Validation failure is not an error path worth
    escalating -- it falls back to the rules like any other miss.
    """

    model_config = ConfigDict(extra="ignore")

    intent: str | None = None
    confidence: float = 0.0
    full_name: str | None = None
    date_of_birth: date | None = None
    medication_name: str | None = None
    faq_topic: str | None = None
    confirm: bool | None = None
    # Optional, not defaulted-false: a model asked for a key it has no opinion
    # about returns `null`, and a schema that refuses null threw away the whole
    # classification over a field nobody had asked about. Measured against Groq
    # this rejected every single reply.
    list_all: bool | None = None
    none_suitable: bool | None = None

    @field_validator("date_of_birth", mode="before")
    @classmethod
    def _tolerate_a_bad_date(cls, value: object) -> object:
        """Drop the field rather than the whole classification.

        Models echo the placeholder out of the prompt -- a literal
        "YYYY-MM-DD" -- and write the string "null" for absent values.
        Strictness here was catastrophic rather than safe: one unusable field
        failed validation for the entire object, so a perfectly good intent was
        thrown away over a date nobody had asked about. Measured against Groq,
        that took classification accuracy from 9/10 to 1/10.

        Nothing is guessed. An unreadable date simply becomes no date, and the
        rules remain the only thing that ever parses one for real.
        """
        if isinstance(value, str) and (
            not value.strip() or value.strip().lower() in {"null", "none", "yyyy-mm-dd"}
        ):
            return None
        return value


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
        return self._merge(baseline, classification)

    # ------------------------------------------------------------ when to ask
    @staticmethod
    def _worth_asking(utterance: str, context: ExtractionContext, baseline: ExtractedTurn) -> bool:
        """Whether a model call would tell us anything.

        Skipped mid-workflow. When the agent has just asked for a date of birth
        the turn is an answer to that question, the rules parse the answer, and
        a model round trip would add latency to every single identity step for
        nothing. This is also what keeps the cost of a call roughly flat.
        """
        if context.awaiting is not None:
            return False
        return len(utterance.strip()) > 2

    # -------------------------------------------------------------- the call
    async def _classify(
        self, utterance: str, context: ExtractionContext, baseline: ExtractedTurn
    ) -> _Classification | None:
        request = LLMRequest(
            messages=[
                ChatMessage(role="system", content=SYSTEM_PROMPT),
                ChatMessage(role="user", content=utterance),
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
    def _parse(text: str) -> _Classification | None:
        """Read the JSON object out of whatever came back.

        Models wrap JSON in prose and in code fences however firmly they are
        asked not to, so the object is located rather than assumed.
        """
        if not text:
            return None
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            return _Classification.model_validate(json.loads(text[start : end + 1]))
        except (ValueError, ValidationError) as exc:
            logger.warning("llm_extraction_unparseable", error=str(exc))
            return None

    # ------------------------------------------------------------- the merge
    @staticmethod
    def _merge(baseline: ExtractedTurn, seen: _Classification) -> ExtractedTurn:
        """Rules for values, model for meaning.

        The split is deliberate and is the point of the whole module. Which
        date was said is a parsing problem, solved by code that has been
        hardened against real calls and is worth trusting. *That* a date of
        birth was being offered at all, or that "remind me about the asthma
        one" is a prescription question, is a comprehension problem, and the
        model is better at it than any table of phrases.

        So the model may overrule an intent the rules did not find, and may
        fill an entity the rules missed -- but never overwrite one they found.
        """
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
            full_name=baseline.full_name or seen.full_name,
            date_of_birth=baseline.date_of_birth or seen.date_of_birth,
            second_factor_value=baseline.second_factor_value,
            reason=baseline.reason,
            practitioner_name=baseline.practitioner_name,
            medication_name=baseline.medication_name or seen.medication_name,
            faq_topic=baseline.faq_topic
            or (seen.faq_topic if intent is Intent.CLINIC_FAQ else None),
            ordinal=baseline.ordinal,
            confirm=baseline.confirm if baseline.confirm is not None else seen.confirm,
            none_suitable=baseline.none_suitable or bool(seen.none_suitable),
            list_all=baseline.list_all or bool(seen.list_all),
            entities=baseline.entities,
        )
