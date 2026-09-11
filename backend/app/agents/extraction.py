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

#: Asking about the cover *this caller* holds, which is a record read and needs
#: verification. Which plans the clinic accepts is a different question with a
#: different answer, held in static configuration and given to anyone who asks
#: (config/clinic.py).
#:
#: The two overlap in almost every word, so the possessive is what separates
#: them: "my insurance" against "what insurance do you take".
PERSONAL_COVERAGE_PHRASES: tuple[str, ...] = (
    "my insurance",
    "my cover",
    "my coverage",
    "my plan",
    "am i covered",
    "am i insured",
    "what insurance do i have",
    "what insurance am i on",
    "insurance on file",
    "my member number",
    "my policy",
)

#: Phrasings that make a question about the clinic however possessive it looks.
#: "Do you take my insurance?" mentions the caller's plan and is really asking
#: which plans are accepted -- answerable without making them verify.
CLINIC_DIRECTED_MARKERS: tuple[str, ...] = (
    "do you take",
    "do you accept",
    "do you support",
    "do you work with",
    "are you in network",
    "in network with",
)


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
    (Intent.COVERAGE_LOOKUP, PERSONAL_COVERAGE_PHRASES),
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
            # Singular as well as plural. The agent's own fallback offers
            # "what your prescription says", so a caller who takes it at its
            # word and asks exactly that was met with the same fallback again
            # -- observed live, four times in a row. Advertising a capability
            # in words the matcher does not accept is worse than not
            # advertising it.
            "my medication",
            "my medications",
            "my prescription",
            "my prescriptions",
            "prescription say",
            "what medication",
            "what medicine",
            # A patient says "how do I use my inhaler", never "how do I take".
            "how do i use",
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

#: Leftmost-longest match over the words above.
#:
#: Both properties are load-bearing, and getting either wrong books the caller
#: into the wrong appointment. "The second one" contains *two* of these words:
#: iterating the dict returned `one` -> 1 before ever testing `second`, so
#: "the second one", "the third one" and "the last one" all resolved to the
#: first offered slot -- confidently, with a confirmation naming a time the
#: caller had not chosen.
#:
#: Leftmost is what disambiguates: the trailing "one" in "the second one" is a
#: pronoun, and the word that actually names the choice comes first. Longest
#: settles "2nd" against a same-position rival. Alternation in a single
#: pattern gives leftmost for free; sorting the branches by length gives
#: longest, because Python tries branches in order at each position.
_ORDINAL_PATTERN = re.compile(
    r"\b(" + "|".join(sorted(map(re.escape, ORDINAL_WORDS), key=len, reverse=True)) + r")\b"
)

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

#: Phrasings that mark a question about the clinic itself, for the case where
#: no FAQ topic matched.
#:
#: Without these, "do you have a physiotherapist?" was classified UNKNOWN and
#: answered with the generic list of things the agent can do -- which reads as
#: a non-answer to someone who asked a perfectly reasonable question. The FAQ
#: workflow has always handled an unanswerable topic by escalating to the front
#: desk (docs/call-flows.md), but nothing could reach that branch: intent only
#: became CLINIC_FAQ once a topic had already matched, and every topic in the
#: alias table has an answer.
#:
#: A small demo vocabulary, in the manner of KNOWN_MEDICATIONS above, and
#: deliberately so: the alternative is a general "is this about the clinic?"
#: rule, which would swallow "what can you help with?" and answer a question
#: about the agent with a transfer to a human.
CLINIC_QUESTION_MARKERS = (
    "do you take",
    "do you accept",
    "do you offer",
    "do you have",
    "do you do",
    "walk in",
    "walk-in",
    "referral",
    "x-ray",
    "xray",
    "blood test",
    "lab work",
    "specialist",
    "physio",
)

#: A small demo vocabulary. Real deployments need the patient's own list or a
#: drug dictionary; the LLM extractor in Phase 12 removes this limitation.
KNOWN_MEDICATIONS = (
    # Not a drug, but what the patient calls one: the record reads "Albuterol
    # inhaler" and the caller says "my inhaler".
    "inhaler",
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

#: Number words, for dates that arrive as speech rather than digits.
#:
#: Speech recognition returns what was said, and people say dates out loud:
#: "the fifteenth of February nineteen eighty five". Every pattern below
#: requires digits, so voice callers could never be verified at all -- the one
#: place in this system where failing to parse means failing to identify a real
#: patient. Deepgram's `smart_format` handles the common shapes, but it mangles
#: some ("nineteen seventy eight" became "19 70 8" in testing), so the text
#: path has to stand on its own.
_UNITS: dict[str, int] = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
    "eleventh": 11,
    "twelfth": 12,
    "thirteenth": 13,
    "fourteenth": 14,
    "fifteenth": 15,
    "sixteenth": 16,
    "seventeenth": 17,
    "eighteenth": 18,
    "nineteenth": 19,
}
_TENS: dict[str, int] = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
    "twentieth": 20,
    "thirtieth": 30,
}
_MONTH_WORDS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
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

        if not any(word in lowered for word in ("appointment", "book", "refill", "cancel")):
            # Before the FAQ, because the FAQ's "insurance" alias would
            # otherwise swallow "what insurance do I have" and answer a
            # question about the caller's own record with a list of the plans
            # the clinic accepts.
            if self._is_personal_coverage(lowered):
                return Intent.COVERAGE_LOOKUP, 0.9
            if self._faq_topic(lowered) is not None:
                return Intent.CLINIC_FAQ, 0.9
            # A clinic question we have no answer for. Routed to the FAQ
            # workflow anyway, with no topic, so it escalates to the front desk
            # rather than being met with the generic capability list.
            if any(marker in lowered for marker in CLINIC_QUESTION_MARKERS):
                return Intent.CLINIC_FAQ, 0.6

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
            if candidate and not _is_spoken_date(candidate.group(1)):
                return " ".join(candidate.group(1).split())
        return None

    @staticmethod
    def _date_of_birth(text: str) -> date | None:
        digitised = _spoken_numbers_to_digits(text)
        for candidate in (text, digitised) if digitised != text else (text,):
            for pattern in _DOB_PATTERNS:
                match = re.search(pattern, candidate, re.IGNORECASE)
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
    def _is_personal_coverage(lowered: str) -> bool:
        """Their cover, not ours."""
        if any(marker in lowered for marker in CLINIC_DIRECTED_MARKERS):
            return False
        return any(phrase in lowered for phrase in PERSONAL_COVERAGE_PHRASES)

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

        match = _ORDINAL_PATTERN.search(lowered)
        if match:
            index = ORDINAL_WORDS[match.group(1)]
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


#: The words that hold a spoken date together but carry no number of their
#: own. Without "thousand", "Twenty First December Two Thousand Two" is not
#: all-date-vocabulary and survives as a name.
_DATE_GLUE = frozenset({"thousand", "hundred", "and", "of", "the", "on", "born"})


def _is_spoken_date(candidate: str) -> bool:
    """Whether a capitalised phrase is really a date being read aloud.

    "Thirtieth April" is two capitalised words, so the bare-name fallback
    claimed it as a full name -- and because a name found this turn overrides
    the one remembered from the last one, a caller who gave their name and then
    their date of birth had the real name replaced by fragments of the date and
    failed verification. Which is how a working call died: name accepted, date
    accepted, identity rejected.

    Every word has to be date vocabulary, not just one. April and June are
    names as well as months, and "April Smith" is a person.
    """
    words = candidate.lower().split()
    return bool(words) and all(
        word in _MONTH_WORDS or word in _UNITS or word in _TENS or word in _DATE_GLUE
        for word in words
    )


def _spoken_numbers_to_digits(text: str) -> str:
    """Rewrite spoken numbers as digits so the date patterns can see them.

    Handles the two shapes that matter for a date of birth: a day ("the
    fifteenth"), and a year said as two pairs ("nineteen eighty five" -> 1985,
    "two thousand and two" -> 2002). Deliberately narrow -- this is not a
    general number parser, and a general one would start rewriting things that
    are not dates.
    """
    words = re.split(r"(\W+)", text.lower())
    out: list[str] = []
    index = 0

    while index < len(words):
        word = words[index]

        # "two thousand and two" / "two thousand"
        if word in _UNITS and _peek(words, index, 2) == "thousand":
            value = _UNITS[word] * 1000
            consumed = 4
            tail = _peek(words, index, 4)
            if tail == "and":
                tail = _peek(words, index, 6)
                consumed = 6
            if tail in _UNITS:
                value += _UNITS[tail]
                consumed += 2
            elif tail in _TENS:
                value += _TENS[tail]
                consumed += 2
                after = _peek(words, index, consumed)
                if after in _UNITS:
                    value += _UNITS[after]
                    consumed += 2
            out.append(f"{value} ")
            index += consumed
            continue

        # "nineteen eighty five" -> 1985; "nineteen seventy" -> 1970
        if word in _UNITS and 10 <= _UNITS[word] <= 19:
            tens = _peek(words, index, 2)
            if tens in _TENS:
                year = _UNITS[word] * 100 + _TENS[tens]
                consumed = 4
                unit = _peek(words, index, 4)
                if unit in _UNITS and _UNITS[unit] < 10:
                    year += _UNITS[unit]
                    consumed += 2
                out.append(f"{year} ")
                index += consumed
                continue

        # "twenty first" -> 21
        if word in _TENS:
            unit = _peek(words, index, 2)
            if unit in _UNITS and _UNITS[unit] < 10:
                out.append(f"{_TENS[word] + _UNITS[unit]} ")
                index += 4
                continue
            out.append(f"{_TENS[word]} ")
            index += 2
            continue

        if word in _UNITS:
            out.append(f"{_UNITS[word]} ")
            index += 2
            continue

        out.append(words[index])
        index += 1

    # Each emitted number carries a trailing space, because consuming a number
    # word also consumes the separator that followed it.
    rewritten = re.sub(r"\s+", " ", "".join(out)).strip()
    # "born on the 15 of February 1985" is not a shape the patterns match; drop
    # the connectives so it becomes "15 February 1985".
    rewritten = re.sub(r"\bthe\s+", "", rewritten)
    rewritten = re.sub(r"\b(\d{1,2}) of (?=[a-z])", r"\1 ", rewritten)
    return rewritten


def _peek(words: list[str], index: int, offset: int) -> str:
    """The token ``offset`` positions ahead, or "" past the end.

    Offsets are even because the split keeps separators: words sit at even
    indices, whitespace at odd ones.
    """
    position = index + offset
    return words[position] if position < len(words) else ""
