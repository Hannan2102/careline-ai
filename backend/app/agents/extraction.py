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
from app.utils.formatting import mentions_a_weekday
from app.workflows.base import AwaitedInput, SlotOffer


@dataclass(frozen=True)
class ExtractedTurn:
    """Fields recovered from one utterance."""

    intent: Intent
    confidence: float
    full_name: str | None = None
    date_of_birth: date | None = None
    second_factor_value: str | None = None
    #: A contact number for somebody being registered, not a factor to check.
    phone: str | None = None
    reason: str | None = None
    practitioner_name: str | None = None
    medication_name: str | None = None
    faq_topic: str | None = None
    ordinal: int | None = None
    confirm: bool | None = None
    none_suitable: bool = False
    list_all: bool = False
    #: A real request that this agent has no way to serve.
    #:
    #: Distinct from UNKNOWN, which means "I could not tell what that was".
    #: This one means the caller was understood perfectly and the answer is
    #: that we do not do it -- a complaint, a test result, a referral chased.
    #: The rules never set it: recognising the limits of the system is exactly
    #: the judgement a phrase table cannot make, so it comes from the model.
    out_of_scope: bool = False
    #: The caller has left the agent's question and asked for something else.
    #:
    #: Never set by the rules. Telling "actually, sort out my repeat while I'm
    #: on" from a clumsy answer to the question just asked is comprehension,
    #: and acting on it wrongly throws away a booking half made.
    changes_subject: bool = False
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
    """How a turn becomes an intent and a set of entities.

    Two entry points because the implementations differ in kind. The rules are
    pure string work and are called synchronously from a hundred tests; an
    extractor that consults a model has to await it. ``aextract`` is what the
    orchestrator uses, and a synchronous implementation satisfies it by
    returning what ``extract`` already produced.
    """

    def extract(self, utterance: str, context: ExtractionContext) -> ExtractedTurn: ...

    async def aextract(self, utterance: str, context: ExtractionContext) -> ExtractedTurn: ...


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
    "my card",
    "my new card",
    "insurance card",
    "plan have you got",
    "plan do you have for me",
    "got down for me",
    "on file for me",
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
    "do you deal with",
    "insurers do you",
    "insurance do you",
)


#: Ways of saying "you don't have me".
#:
#: All assertions about *being* new, or asking to become a patient — never
#: questions about the process, which belong to the FAQ and beat these on
#: length ("new patient what", "as a new patient").
NEW_PATIENT_PHRASES: tuple[str, ...] = (
    "new patient",
    "not been before",
    "never been to",
    "never been before",
    "not registered",
    "register with",
    "register me",
    "registering as",
    "sign up as a patient",
    "join the practice",
    "join your practice",
    "become a patient",
    "get on your books",
    "i'm new here",
    "i am new here",
    "first time calling",
    "not a patient yet",
)


INTENT_PHRASES: tuple[tuple[Intent, tuple[str, ...]], ...] = (
    (
        Intent.REFILL_REQUEST,
        (
            "refill",
            "repeat prescription",
            "my repeat",
            # Not "run out of": people say "I've nearly run out" and stop
            # there, and requiring the preposition meant the commonest
            # phrasing of the commonest request matched nothing at all.
            "run out",
            "ran out",
            "running out",
            "running low",
            "getting low",
            "more of my",
            "top up my prescription",
            "send more",
            "the pharmacy",
            "another month",
            "another box",
            "another pack",
            "renew",
            "my script",
            "new script",
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
            "how often",
            "the dose",
            "the dosage",
            "meant to take",
            "meant to be taking",
            "supposed to be taking",
            "with food",
            "the label",
            "label say",
            # What is on the box, described the way it is held: callers name
            # the tablet, not the molecule. None of these name a drug -- they
            # only establish that the question is about one -- so the workflow
            # reads the record back and asks which was meant. Deciding that
            # "the sugar one" is the metformin would be a clinical inference,
            # and a wrong one reads out the wrong dosage.
            "tablet",
            "pill",
            "capsule",
            "puffer",
            "the sugar",
            "water tablet",
            "blood pressure one",
            "asthma one",
            "chest one",
            "cholesterol one",
            "thyroid one",
            "white ones",
            "blue one",
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
            "change the day",
            "change the date",
            "change the time",
            "different day",
            "swap my appointment",
            "shift my appointment",
        ),
    ),
    (
        Intent.CANCEL_APPOINTMENT,
        (
            "cancel",
            "call off",
            "can't make it",
            "cannot make it",
            "won't be able to make",
            "won't be there",
            "can't be there",
            "not going to make",
            "take me off",
            "something's come up",
            "something has come up",
            "something came up",
        ),
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
            "did i book",
            "am i booked",
            "am i down for",
            "am i on the list",
            "what appointments",
            "appointments do i have",
            "my appointments",
            "anything booked",
            "what time is my",
            "when am i due",
            "due in",
        ),
    ),
    (Intent.NEW_PATIENT, NEW_PATIENT_PHRASES),
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
            "see someone",
            "to be seen",
            "be seen before",
            "can i get in",
            "get me in",
            "fit me in",
            "squeeze me in",
            "set me up",
            "put me down for",
            "next opening",
            "any openings",
            "next available",
            "earliest you",
            "check-up",
            "checkup",
            "check up",
            "follow-up",
            "follow up",
            # A medication review is an appointment, however much it sounds
            # like a question about the prescription itself.
            "prescription check",
            "medication review",
            # Asking after a cancelled slot is asking for one, not giving one
            # up. Same word, opposite request.
            "any cancellation",
            "do you have anything",
            "any appointments",
            "appointments free",
            "any availability",
            "anything free",
        ),
    ),
)

#: Words that belong to exactly one request, whatever else is in the sentence.
#:
#: The scoring below prefers the longest match, which is a good proxy for
#: specificity and a bad one here: "I need a refill on my prescription"
#: contains "my prescription" (fifteen characters, a dosage question) and
#: "refill" (six, and decisive). Nobody says "refill" about anything but a
#: refill, so length is simply the wrong measure for these few, and they are
#: settled before it is applied.
DECISIVE_PHRASES: tuple[tuple[str, Intent], ...] = (
    ("refill", Intent.REFILL_REQUEST),
    ("repeat prescription", Intent.REFILL_REQUEST),
    ("running low", Intent.REFILL_REQUEST),
    ("getting low", Intent.REFILL_REQUEST),
    ("running out", Intent.REFILL_REQUEST),
    ("run out", Intent.REFILL_REQUEST),
    ("ran out", Intent.REFILL_REQUEST),
    ("reschedule", Intent.RESCHEDULE_APPOINTMENT),
)

#: A cancellation that names a replacement is a reschedule.
#:
#: "Scrap Thursday and give me Friday" is one request, and reading it as a
#: cancellation loses the half the caller cared about: they hang up believing
#: they have Friday, and they have nothing. No single phrase decides it --
#: what makes it a reschedule is that a *drop* and an *offer* both appear, in
#: either order, which is a shape rather than a word.
_RESCHEDULE_PAIR = re.compile(
    r"\b(?:cancel|scrap|drop|call off|take me off|move|change)\b.{0,60}"
    r"\b(?:instead|and (?:give|book|make|put|set|do)|rebook)\b"
    r"|\b(?:instead of|rather than)\b.{0,60}\b(?:cancel|scrap|drop)\b"
)

#: "I've got two left." "Only a few days left."
#:
#: A count of what remains is a refill request that never uses the word, and
#: it is how somebody who has never said "refill" in their life asks for one.
_RUNNING_LOW = re.compile(
    r"\b(?:only |just |about )?(?:a few|a couple of|one|two|three|four|five|six|seven|\d+)\s+"
    r"(?:tablets?|pills?|doses?|days?|weeks?)?\s*(?:left|remaining|to go)\b"
)

#: Clinic-fact aliases that ordinary appointment talk swallows whole.
#:
#: "Saturday" is the entire question in "are you open Saturday?" and merely
#: incidental in "cancel my Saturday appointment". "Open" is the topic in
#: "what time do you open" and part of the request in "any openings?".
#: Specificity alone cannot separate those, because the alias really is the
#: longer match in the sentence where it is beside the point -- so these are
#: consulted only when nothing else the caller said was about their own
#: booking or prescription. The strong aliases need no such protection:
#: "cancellation policy" and "how much will this cost" are specific enough to
#: win on length honestly.
WEAK_FAQ_ALIASES = frozenset(
    {
        "open",
        "saturday",
        "sunday",
        "weekend",
        "number",
        "early",
        "cancel",
        "cancellation",
        "covered",
        "insured",
        "insurance",
        "coverage",
        "price",
        "bring",
        "doctors",
        "providers",
    }
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
        "go on",
        "please do",
        "that's fine",
        "that is fine",
        "alright",
        "all right",
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
        # Ways of saying "that's me finished", for the question the agent asks
        # at the end of everything. Read as a no wherever they appear, which
        # is harmless: none of them is a plausible yes to anything else.
        "that's all",
        "that is all",
        "that's everything",
        "that's it",
        "nothing else",
        "no more",
        "all set",
        "we're done",
        "i'm done",
        "leave it",
        "forget it",
        "hold on",
        "wait",
    }
)
#: Ways of saying "not one of those".
#:
#: Only consulted while times are on the table, which is what makes the looser
#: phrasings safe: "anything else" is a request for other times when it
#: follows a list of them, whatever it means anywhere else.
NONE_SUITABLE = (
    "none of those",
    "none of them",
    "none of these",
    "none work",
    "nothing works",
    "nothing suits",
    "neither",
    "any other",
    "anything else",
    "something else",
    "what else",
    "other times",
    "other days",
    "another day",
    "doesn't work",
    "don't work",
    "won't work",
    "no good",
    "too early",
    "too late",
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

#: The words above that are ordinary numbers rather than positions. "Second"
#: can only be a choice; "two" is a choice, a dose, a year and a pronoun.
CARDINALS = frozenset({"one", "two", "three"})

#: Words that can sit in front of a spoken number without making it a pronoun.
_POSITION_LEAD_IN = frozenset({"number", "option", "choice", "the", "just", "make", "it"})

#: And words that can follow one without qualifying it into a pronoun.
_POSITION_TAIL = frozenset({"please", "thanks", "thank", "you", "then", "ok", "okay"})

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

#: Commas and full stops, for matching a phrase that spans one.
_PUNCTUATION = re.compile(r"[,.;:!?]")

#: "Oh" is a zero when someone is reading digits aloud, and an ordinary noise
#: everywhere else -- so it is substituted only on the second-factor path,
#: never in a date or a name.
_OH_AS_ZERO = re.compile(r"\boh\b")

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

#: Words for a thing, not guesses at which thing.
#:
#: A puffer is an inhaler -- same object, different word -- so translating it
#: costs nothing and saves the caller being told we have no prescription for a
#: "puffer". Nothing here crosses from a description to a drug: "the sugar
#: one" is deliberately absent, because deciding it means the metformin is a
#: clinical inference, and the wrong inference reads out the wrong dosage. The
#: descriptive phrases are matched as *intent* only (see INTENT_PHRASES
#: above), which leaves the workflow to read the record back and ask which was
#: meant -- the answer a receptionist would give.
MEDICATION_SYNONYMS: tuple[tuple[str, str], ...] = (
    ("puffer", "inhaler"),
    ("pump", "inhaler"),
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

#: A month, however much of it was said.
#:
#: Three letters, then whatever else came: "feb", "Feb.", "february" and
#: "sept" all match, and a caller who abbreviates is not asked to repeat
#: themselves. Observed live -- "fifteenth Feb nineteen eighty five" parsed to
#: nothing, and the caller was asked for their date of birth again.
_MONTH_RE = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"

#: The same month vocabulary, anchored, for testing one word at a time.
_MONTH_FULLMATCH = re.compile(_MONTH_RE, re.IGNORECASE)

_DOB_PATTERNS = (
    r"\b(\d{4}-\d{2}-\d{2})\b",
    rf"\b(\d{{1,2}}\s+{_MONTH_RE}\s+\d{{4}})\b",
    rf"\b({_MONTH_RE}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}})\b",
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
            phone=self._phone(lowered, context),
            reason=self._reason(text, intent, context),
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
                    "meant to be taking",
                    "supposed to be taking",
                    "what am i on",
                    "what do i take",
                    "everything i",
                    "all of my",
                )
            ),
        )

    # ------------------------------------------------------------- intent
    def _intent(self, lowered: str, context: ExtractionContext) -> tuple[Intent, float]:
        """Route the utterance by the most specific thing in it.

        Mid-workflow the outstanding question decides instead: re-classifying
        every turn would let "yes" restart a booking.

        Everywhere else the rule is **longest match wins**, which is the rule
        the ordinal matcher above had to learn the same way. A flat table read
        in order answers "which phrase did somebody happen to list first", and
        that was wrong in both directions at once: "Did I book something?"
        opened a booking because it contains "book", and "Got any
        cancellations?" cancelled an appointment because it contains "cancel".
        Length is a fair proxy for specificity, and -- unlike table order -- it
        does not quietly change what a sentence means when a phrase is
        appended to the end of an unrelated list.

        Ties fall to whichever table was consulted first, so the order below is
        still a priority: the caller's own cover before the clinic's brochure,
        before the appointment verbs, before a bare "do you...?".
        """
        if context.awaiting is not None:
            # One exception, and it is one the agent asks for: the identity
            # prompt says "if you've not been to the clinic before, say so and
            # I can register you". A caller who says it must be heard, and it
            # cannot be an answer to "what is your name and date of birth" --
            # so hearing it costs nothing that mid-workflow silence protects.
            if any(phrase in lowered for phrase in NEW_PATIENT_PHRASES):
                return Intent.NEW_PATIENT, 0.9
            return Intent.UNKNOWN, 1.0

        # Two requests that no single phrase settles, because what makes each
        # one what it is, is that two separate parts both appear.
        if _RESCHEDULE_PAIR.search(lowered):
            return Intent.RESCHEDULE_APPOINTMENT, 0.9
        if _RUNNING_LOW.search(lowered):
            return Intent.REFILL_REQUEST, 0.9
        for phrase, decided in DECISIVE_PHRASES:
            if phrase in lowered:
                return decided, 0.9

        # Punctuation removed for matching, because a comma in a transcript is
        # the recogniser's decision rather than the caller's: "I'm a new
        # patient, what happens" and the same sentence without the comma are
        # the same question, and a phrase spanning it would match only one.
        plain = " ".join(_PUNCTUATION.sub(" ", lowered).split())

        best: tuple[Intent, float] | None = None
        longest = 0
        for intent, phrases, confidence in self._tables(plain):
            for phrase in phrases:
                if len(phrase) > longest and phrase in plain:
                    longest, best = len(phrase), (intent, confidence)
        if best is None:
            return Intent.UNKNOWN, 0.2

        # Being new outranks what they want to do with it. "I've never been
        # before, can I get an appointment?" scores as a booking on length --
        # and a booking sends somebody with no record to prove who they are,
        # fail, and be handed to the front desk. Registering them first is the
        # only route that ends anywhere.
        if best[0] is Intent.BOOK_APPOINTMENT and any(
            phrase in plain for phrase in NEW_PATIENT_PHRASES
        ):
            return Intent.NEW_PATIENT, 0.9
        return best

    def _tables(self, lowered: str) -> tuple[tuple[Intent, tuple[str, ...], float], ...]:
        """Every phrase that could claim this utterance, in priority order."""
        return (
            # Before the FAQ, whose "insurance" alias would otherwise swallow
            # "what insurance do I have" and answer a question about the
            # caller's own record with a list of the plans the clinic accepts.
            (
                Intent.COVERAGE_LOOKUP,
                () if self._is_clinic_directed(lowered) else PERSONAL_COVERAGE_PHRASES,
                0.9,
            ),
            (Intent.CLINIC_FAQ, self._faq_phrases(lowered), 0.9),
            # Coverage is skipped here: it is the table above, where the
            # clinic-directed veto applies to it. Scanned again as part of
            # INTENT_PHRASES it would match "do you take my insurance" and
            # send a caller asking a public question off to prove who they are.
            *(
                (intent, phrases, 0.9)
                for intent, phrases in INTENT_PHRASES
                if intent is not Intent.COVERAGE_LOOKUP
            ),
            # A clinic question we have no answer for. Routed to the FAQ
            # workflow anyway, with no topic, so it escalates to the front desk
            # rather than being met with the generic capability list.
            (Intent.CLINIC_FAQ, CLINIC_QUESTION_MARKERS, 0.6),
        )

    def _faq_phrases(self, lowered: str) -> tuple[str, ...]:
        """The clinic-fact alias that matched, as the caller actually said it.

        The matched text rather than the topic it maps to, so that it competes
        on the same footing as everything else: "how much will this cost" is a
        longer and more specific claim on the utterance than "how much", which
        is exactly why it should win.
        """
        alias = self._faq_topic(lowered)
        if alias is None:
            return ()
        if alias in WEAK_FAQ_ALIASES and self._mentions_their_own(lowered):
            return ()
        return (alias.replace("_", " "),)

    @staticmethod
    def _mentions_their_own(lowered: str) -> bool:
        """Whether anything in the utterance is about this caller's own records."""
        return any(phrase in lowered for _intent, phrases in INTENT_PHRASES for phrase in phrases)

    async def aextract(self, utterance: str, context: ExtractionContext) -> ExtractedTurn:
        """The rules again. Nothing here waits on anything."""
        return self.extract(utterance, context)

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
                return _without_leading_ordinal(candidate.group(1))
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
    def _phone(lowered: str, context: ExtractionContext) -> str | None:
        """A whole phone number, taken down for the first time.

        Only when it was asked for. Numbers turn up everywhere in this domain
        -- a year of birth, a dose, "the second one" -- and a rule that
        collected any long run of digits would file a date of birth as a
        contact number.

        Spoken digits first, because nobody reads a phone number as a number:
        "five five five, oh one nine, oh one four two" arrives with one digit
        character in it.
        """
        if context.awaiting is not AwaitedInput.PHONE:
            return None
        spoken = _spoken_numbers_to_digits(_OH_AS_ZERO.sub("0", lowered))
        digits = re.sub(r"\D", "", spoken)
        return digits if len(digits) >= 10 else None

    @staticmethod
    def _second_factor(lowered: str, context: ExtractionContext) -> str | None:
        if context.awaiting is not AwaitedInput.SECOND_FACTOR:
            return None
        # Nobody reads a phone number as a number. "Zero four one one" arrives
        # with no digits in it at all, so stripping non-digits left nothing and
        # the agent asked for the same four digits indefinitely -- observed
        # live, three times in a row before the caller gave up.
        spoken = _spoken_numbers_to_digits(_OH_AS_ZERO.sub("0", lowered))
        digits = re.sub(r"\D", "", spoken)
        return digits[-4:] if len(digits) >= 4 else None

    @staticmethod
    def _reason(text: str, intent: Intent, context: ExtractionContext) -> str | None:
        # When the agent has just asked what the visit is for, the answer is
        # the reason, whatever intent the turn scores as. Mid-workflow turns
        # are UNKNOWN by design, so requiring BOOK_APPOINTMENT here meant "my
        # knee has been hurting" was not a reason -- and the workflow asked
        # the same question again, and again.
        if context.awaiting is AwaitedInput.REASON:
            return text or None
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
        for spoken, recorded in MEDICATION_SYNONYMS:
            if spoken in lowered:
                return recorded
        return None

    @staticmethod
    def _is_clinic_directed(lowered: str) -> bool:
        """Whether a question that mentions insurance is about *ours*.

        "Do you take my insurance?" names the caller's plan and is really
        asking which plans are accepted -- answerable without making anyone
        prove who they are.
        """
        return any(marker in lowered for marker in CLINIC_DIRECTED_MARKERS)

    @staticmethod
    def _faq_topic(lowered: str) -> str | None:
        """The most specific clinic-fact alias in the utterance.

        Matched against the words with punctuation removed, because a caller's
        comma is the recogniser's decision rather than theirs: "I'm a new
        patient, what happens?" and "I'm a new patient what happens" are the
        same question, and an alias that spans the comma would match only one
        of them.
        """
        plain = _PUNCTUATION.sub(" ", lowered)
        plain = " ".join(plain.split())
        for alias in sorted(FAQ_TOPIC_ALIASES, key=len, reverse=True):
            if alias.replace("_", " ") in plain:
                return alias
        return None

    @staticmethod
    def _ordinal(lowered: str, context: ExtractionContext) -> int | None:
        # A named day settles it on its own, and settles it either way.
        #
        # It is the most specific thing a caller can say -- more specific than
        # any number in the sentence -- so if one is named, nothing else gets
        # to answer. Falling through to the ordinal when the day matched
        # nothing is how "can you cancel the Tuesday one?" resolved to the
        # first appointment on the list, which was a Wednesday: there was no
        # Tuesday, and the trailing "one" is a pronoun, not a number. Saying a
        # day we cannot find has to mean asking again.
        if mentions_a_weekday(lowered):
            for offer in context.offers:
                if offer.start.strftime("%A").lower() in lowered:
                    return offer.index
            return None

        match = re.search(r"\b(?:option|number|choice)?\s*([1-9])\b", lowered)
        if match:
            return int(match.group(1))

        match = _ORDINAL_PATTERN.search(lowered)
        if match:
            if match.group(1) in CARDINALS and not _reads_as_a_position(
                lowered, match.start(), match.end()
            ):
                # "One" is a number in "number one" and a pronoun in "the
                # Tuesday one", "the one after that", "the early one". Reading
                # the pronoun as the number picks the first of whatever was
                # offered -- confidently, and with no relation to what was
                # asked for. Nothing is guessed instead: the turn goes to the
                # model, and failing that the agent asks again.
                return None
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


def _reads_as_a_position(lowered: str, start: int, end: int) -> bool:
    """Whether a plain number is a choice from a list or a pronoun.

    A choice stands alone: "two, please", "number two", "the one". A pronoun
    is qualified by something, and the qualifier is what does the choosing --
    "the *Tuesday* one", "the one *after that*", "the *early* one". So the
    test is whether anything either side is doing work.

    Getting this wrong the other way is the expensive direction: every one of
    those pronoun phrasings resolved to the first thing on the list, which for
    "the Tuesday one" meant offering to cancel a Wednesday appointment.
    """
    before = re.findall(r"[a-z']+", lowered[:start])
    after = re.findall(r"[a-z']+", lowered[end:])
    leads = not before or before[-1] in _POSITION_LEAD_IN
    trails = all(word in _POSITION_TAIL for word in after)
    return leads and trails


def _without_leading_ordinal(candidate: str) -> str:
    """Drop a stray ordinal in front of a name.

    Recognisers hallucinate a leading ordinal surprisingly often on a short
    reply -- "Fifth John Smith" was transcribed live, from someone saying only
    their name, and it failed verification because the record is under "John
    Smith". Nobody's first name is an ordinal, so removing one costs nothing.

    Ordinals only, never month words: April, May and June are first names, and
    stripping those would turn "April Smith" into "Smith".
    """
    words = candidate.split()
    # The full number vocabulary, not ORDINAL_WORDS: that table stops at three
    # because it only ever has to resolve a choice from a short list of offered
    # slots, and "Fifth" is exactly the kind of thing that gets hallucinated.
    while len(words) > 2 and (
        words[0].lower() in ORDINAL_WORDS or words[0].lower() in _UNITS or words[0].lower() in _TENS
    ):
        words = words[1:]
    return " ".join(words)


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

    The month is matched however much of it was said, for the same reason the
    date patterns are: people abbreviate out loud, and Deepgram writes down
    what it heard. "Fifteenth Feb nineteen eighty five" was being claimed as a
    caller named *Fifteenth Feb* -- overwriting the real name given the turn
    before -- because the full-month list has no "feb" in it. The date parsed
    correctly the whole time; the name it arrived with is what failed.
    """
    words = candidate.lower().split()
    return bool(words) and all(
        _MONTH_FULLMATCH.fullmatch(word) or word in _UNITS or word in _TENS or word in _DATE_GLUE
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
