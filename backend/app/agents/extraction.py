"""Turning an utterance into typed fields.

Workflows take typed input; something has to produce it. This module defines
that boundary and ships the deterministic implementation used in mock mode --
regexes and a small vocabulary, no model, no cost.

An LLM implementation of the same Protocol arrives in Phase 12 and will handle
the paraphrases these rules miss. The point of the seam is that swapping them
changes nothing else: workflows already receive typed fields either way, and
the safety layer runs before extraction regardless, so a missed extraction can
only ever produce "I didn't understand", never an unsafe action.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from dateutil import parser as date_parser

from app.agents.intents import Intent
from app.config.clinic import FAQ_TOPIC_ALIASES, PRACTITIONERS
from app.workflows.base import AwaitedInput, SlotOffer


@dataclass(frozen=True)
class ExtractedTurn:
    """Fields recovered from one utterance."""

    intent: Intent
    confidence: float
    full_name: str | None = None
    date_of_birth: date | None = None
    second_factor_value: str | None = None
    reason: str | None = None
    practitioner_name: str | None = None
    medication_name: str | None = None
    faq_topic: str | None = None
    ordinal: int | None = None
    confirm: bool | None = None
    none_suitable: bool = False
    list_all: bool = False
    entities: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ExtractionContext:
    """What the orchestrator already knows, so references can be resolved.

    "I'd like Tuesday" is meaningful only against the slots just offered, which
    is why they are passed in rather than re-derived.
    """

    awaiting: AwaitedInput | None = None
    offers: tuple[SlotOffer, ...] = ()
    known_medications: tuple[str, ...] = ()


class TurnExtractor(Protocol):
    def extract(self, utterance: str, context: ExtractionContext) -> ExtractedTurn: ...


# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

INTENT_PHRASES: tuple[tuple[Intent, tuple[str, ...]], ...] = (
    (
        Intent.REFILL_REQUEST,
        (
            "refill",
            "repeat prescription",
            "run out of",
            "running out of",
            "more of my",
            "top up my prescription",
        ),
    ),
    (
        Intent.MEDICATION_LOOKUP,
        (
            "how much",
            "what dose",
            "what dosage",
            "how many",
            "how do i take",
            "when do i take",
            "supposed to take",
            "what am i taking",
            "my medications",
            "my prescriptions",
            "what did my doctor prescribe",
            "prescribed for",
        ),
    ),
    (
        Intent.RESCHEDULE_APPOINTMENT,
        (
            "reschedule",
            "move my appointment",
            "change my appointment",
            "move it",
            "different time",
            "another time",
            "push it back",
            "bring it forward",
        ),
    ),
    (
        Intent.CANCEL_APPOINTMENT,
        ("cancel", "call off", "can't make it", "cannot make it", "won't be able to make"),
    ),
    (
        Intent.LOOKUP_APPOINTMENT,
        (
            "when is my appointment",
            "when's my appointment",
            "who am i seeing",
            "where is my appointment",
            "do i have an appointment",
            "check my appointment",
            "confirm my appointment",
            "my next appointment",
        ),
    ),
    (
        Intent.BOOK_APPOINTMENT,
        (
            "book",
            "schedule",
            "make an appointment",
            "come in",
            "see the doctor",
            "see a doctor",
            "get an appointment",
            "need an appointment",
            "like an appointment",
        ),
    ),
)

YES_WORDS = frozenset(
    {
        "yes",
        "yeah",
        "yep",
        "yup",
        "sure",
        "ok",
        "okay",
        "please",
        "go ahead",
        "that works",
        "sounds good",
        "correct",
        "confirm",
        "do it",
        "book it",
        "perfect",
    }
)
NO_WORDS = frozenset(
    {
        "no",
        "nope",
        "nah",
        "not really",
        "don't",
        "do not",
        "cancel that",
        "never mind",
        "hold on",
        "wait",
    }
)
NONE_SUITABLE = (
    "none of those",
    "none of them",
    "nothing works",
    "neither",
    "any other",
    "something else",
    "anything else that",
    "other times",
    "doesn't work",
    "won't work",
)

ORDINAL_WORDS: dict[str, int] = {
    "first": 1,
    "1st": 1,
    "one": 1,
    "second": 2,
    "2nd": 2,
    "two": 2,
    "third": 3,
    "3rd": 3,
    "three": 3,
    "last": -1,
}

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

#: A small demo vocabulary. Real deployments need the patient's own list or a
#: drug dictionary; the LLM extractor in Phase 12 removes this limitation.
KNOWN_MEDICATIONS = (
    "metformin",
    "lisinopril",
    "albuterol",
    "atorvastatin",
    "amoxicillin",
    "ibuprofen",
    "paracetamol",
    "insulin",
    "amlodipine",
    "levothyroxine",
)

#: The lead-in is case-insensitive; the name itself is not. Requiring capitals
#: on the name is what stops "i'm going to need an appointment" being read as a
#: person called "Going To".
_NAME_PATTERNS = (
    r"(?i:my name'?s|my name is|i am|i'?m|this is|it'?s|speaking with|calling for)"
    r"\s+([A-Z][\w'-]+(?:\s+[A-Z][\w'-]+)+)",
    r"^([A-Z][\w'-]+(?:\s+[A-Z][\w'-]+)+)[,.]?\s*(?i:born|date of birth|dob)",
)

_DOB_PATTERNS = (
    r"\b(\d{4}-\d{2}-\d{2})\b",
    r"\b(\d{1,2}\s+(?:january|february|march|april|may|june|july|august|september|october|"
    r"november|december)\s+\d{4})\b",
    r"\b((?:january|february|march|april|may|june|july|august|september|october|november|"
    r"december)\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})\b",
    r"\b(\d{1,2}/\d{1,2}/\d{4})\b",
)


class RuleBasedExtractor:
    """Deterministic extraction. The mock-mode implementation of the seam."""

    def extract(self, utterance: str, context: ExtractionContext) -> ExtractedTurn:
        text = utterance.strip()
        lowered = text.lower()

        intent, confidence = self._intent(lowered, context)
        return ExtractedTurn(
            intent=intent,
            confidence=confidence,
            full_name=self._name(text, context),
            date_of_birth=self._date_of_birth(text),
            second_factor_value=self._second_factor(lowered, context),
            reason=self._reason(text, intent),
            practitioner_name=self._practitioner(lowered),
            medication_name=self._medication(lowered, context),
            faq_topic=self._faq_topic(lowered),
            ordinal=self._ordinal(lowered, context),
            confirm=self._confirmation(lowered),
            none_suitable=any(phrase in lowered for phrase in NONE_SUITABLE),
            list_all=any(
                phrase in lowered
                for phrase in (
                    "what am i taking",
                    "my medications",
                    "my prescriptions",
                    "what medications",
                    "list my",
                )
            ),
        )

    # ------------------------------------------------------------- intent
    def _intent(self, lowered: str, context: ExtractionContext) -> tuple[Intent, float]:
        # Mid-workflow, the outstanding question determines what this turn is
        # about. Re-classifying every turn would let "yes" restart a booking.
        if context.awaiting is not None:
            return Intent.UNKNOWN, 1.0

        if self._faq_topic(lowered) is not None and not any(
            word in lowered for word in ("appointment", "book", "refill", "cancel")
        ):
            return Intent.CLINIC_FAQ, 0.9

        for intent, phrases in INTENT_PHRASES:
            for phrase in phrases:
                if phrase in lowered:
                    return intent, 0.9
        return Intent.UNKNOWN, 0.2

    # ----------------------------------------------------------- entities
    @staticmethod
    def _name(text: str, context: ExtractionContext) -> str | None:
        for pattern in _NAME_PATTERNS:
            match = re.search(pattern, text, re.MULTILINE)
            if match:
                return " ".join(match.group(1).split())

        # When a name is what we asked for, a bare "John Smith" is an answer.
        if context.awaiting is AwaitedInput.IDENTITY:
            candidate = re.match(r"^\s*([A-Z][\w'-]+(?:\s+[A-Z][\w'-]+)+)", text.strip())
            if candidate:
                return " ".join(candidate.group(1).split())
        return None

    @staticmethod
    def _date_of_birth(text: str) -> date | None:
        for pattern in _DOB_PATTERNS:
            match = re.search(pattern, text, re.IGNORECASE)
            if not match:
                continue
            try:
                # dayfirst: the clinic is fictional, but "15/02/1985" is far
                # more likely to be 15 February than an invalid month.
                return date_parser.parse(match.group(1), dayfirst=True).date()
            except (ValueError, OverflowError):
                continue
        return None

    @staticmethod
    def _second_factor(lowered: str, context: ExtractionContext) -> str | None:
        if context.awaiting is not AwaitedInput.SECOND_FACTOR:
            return None
        digits = re.sub(r"\D", "", lowered)
        return digits[-4:] if len(digits) >= 4 else None

    @staticmethod
    def _reason(text: str, intent: Intent) -> str | None:
        if intent is not Intent.BOOK_APPOINTMENT:
            return None
        # The whole utterance is the reason; classify_reason narrows it to a
        # visit type, and keeping the original text preserves the visit note.
        return text or None

    @staticmethod
    def _practitioner(lowered: str) -> str | None:
        match = re.search(r"\b(?:dr\.?|doctor)\s+([a-z][\w'-]+)", lowered)
        if match:
            return match.group(1)
        for practitioner in PRACTITIONERS:
            if practitioner.family_name.lower() in lowered:
                return practitioner.family_name
        return None

    @staticmethod
    def _medication(lowered: str, context: ExtractionContext) -> str | None:
        vocabulary = tuple(m.lower() for m in context.known_medications) or KNOWN_MEDICATIONS
        for name in vocabulary:
            head = name.split()[0]
            if head in lowered:
                return head
        return None

    @staticmethod
    def _faq_topic(lowered: str) -> str | None:
        for alias in sorted(FAQ_TOPIC_ALIASES, key=len, reverse=True):
            if alias.replace("_", " ") in lowered:
                return alias
        return None

    @staticmethod
    def _ordinal(lowered: str, context: ExtractionContext) -> int | None:
        # A weekday only means something against the times just offered.
        if context.offers:
            for offer in context.offers:
                if offer.start.strftime("%A").lower() in lowered:
                    return offer.index

        match = re.search(r"\b(?:option|number|choice)?\s*([1-9])\b", lowered)
        if match:
            return int(match.group(1))

        for word, index in ORDINAL_WORDS.items():
            if re.search(rf"\b{re.escape(word)}\b", lowered):
                if index == -1:
                    return len(context.offers) or None
                return index
        return None

    @staticmethod
    def _confirmation(lowered: str) -> bool | None:
        stripped = lowered.strip(" .!?,")
        if any(re.search(rf"\b{re.escape(word)}\b", stripped) for word in NO_WORDS):
            return False
        if any(re.search(rf"\b{re.escape(word)}\b", stripped) for word in YES_WORDS):
            return True
        return None
