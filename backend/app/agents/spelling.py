"""Names, letter by letter.

A name is the half of an identity that speech recognition gets wrong most
quietly. "Smith" and "Smyth", "Jon" and "John", "Catherine" and "Kathryn" are
the same sound, and the recogniser writes down whichever spelling it likes
best. The caller never sees what was written, so they have no way to correct
it -- and verification then fails as though they had given the wrong details.
Found in a demo: a caller who was certainly on file failed twice before anyone
realised the agent had been searching for a spelling nobody had said.

So the agent spells the name back, and the caller can spell it again. This
module is both halves of that:

* **saying a name letter by letter** -- a written form for the transcript and
  the dashboard ("J-O-H-N"), and a spoken form for the speech engine;
* **reading a spelling** -- letters as a recogniser actually transcribes them:
  capitals ("S M Y T H"), joined ("s-m-y-t-h", "S. M. Y. T. H."), homophones
  ("see", "are", "why", "double you"), "as in" ("S as in Sam"), and the NATO
  alphabet ("sierra mike yankee").

Deterministic and pure. The model may notice that a caller is spelling; it is
never the thing that decides which letters they said (ADR 008).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import pairwise

#: Characters inside a name that are not letters, and how they are said.
SPECIAL_CHARACTERS: dict[str, str] = {"'": "apostrophe", "-": "hyphen", " ": "space"}

#: Words a recogniser writes for a letter that was said on its own.
#:
#: Deepgram transcribes a spelled name as capitals some of the time and as the
#: word it heard the rest -- "see" for C, "are" for R, "why" for Y. Only the
#: spellings that are not already a single letter are listed.
LETTER_WORDS: dict[str, str] = {
    "ay": "A",
    "aye": "I",
    "bee": "B",
    "be": "B",
    "see": "C",
    "sea": "C",
    "cee": "C",
    "dee": "D",
    "ee": "E",
    "ef": "F",
    "eff": "F",
    "gee": "G",
    "jee": "G",
    "aitch": "H",
    "haitch": "H",
    "eye": "I",
    "jay": "J",
    "kay": "K",
    "el": "L",
    "ell": "L",
    "em": "M",
    "en": "N",
    "oh": "O",
    "owe": "O",
    "pee": "P",
    "pea": "P",
    "queue": "Q",
    "cue": "Q",
    "are": "R",
    "ar": "R",
    "es": "S",
    "ess": "S",
    "tee": "T",
    "tea": "T",
    "you": "U",
    "yew": "U",
    "vee": "V",
    "ex": "X",
    "why": "Y",
    "wye": "Y",
    "zee": "Z",
    "zed": "Z",
}

NATO_ALPHABET: dict[str, str] = {
    "alpha": "A",
    "alfa": "A",
    "bravo": "B",
    "charlie": "C",
    "delta": "D",
    "echo": "E",
    "foxtrot": "F",
    "golf": "G",
    "hotel": "H",
    "india": "I",
    "juliet": "J",
    "juliett": "J",
    "kilo": "K",
    "lima": "L",
    "mike": "M",
    "november": "N",
    "oscar": "O",
    "papa": "P",
    "quebec": "Q",
    "romeo": "R",
    "sierra": "S",
    "tango": "T",
    "uniform": "U",
    "victor": "V",
    "whiskey": "W",
    "whisky": "W",
    "xray": "X",
    "yankee": "Y",
    "zulu": "Z",
}

_SPOKEN_SPECIALS: dict[str, str] = {
    "hyphen": "-",
    "dash": "-",
    "apostrophe": "'",
    "space": " ",
}

#: Words a caller puts around a spelling without being part of it: "no, it's
#: spelled S M Y T H", "my last name is S M Y T H, thanks". Deliberately
#: excludes every word that is also a letter -- "you", "are", "why", "oh" --
#: because inside a spelling those are letters.
_LEAD_INS = frozenset(
    {
        "no",
        "nope",
        "nah",
        "sorry",
        "um",
        "uh",
        "er",
        "erm",
        "so",
        "okay",
        "ok",
        "yes",
        "yeah",
        "actually",
        "it's",
        "its",
        "it",
        "is",
        "that's",
        "that",
        "spelled",
        "spelt",
        "spell",
        "spelling",
        "my",
        "the",
        "last",
        "first",
        "middle",
        "given",
        "family",
        "surname",
        "name",
        "name's",
        "names",
        "should",
        "be",
        "like",
        "with",
        "letters",
    }
)
_TRAILERS = frozenset({"please", "thanks", "thank", "you", "cheers"})

#: Words that can sit between a letter and its example: "S as in Sam", "S for
#: Sam", "S like Sam".
_AS_IN = (("as", "in"), ("for",), ("like",))

#: Ignored between letters. "Capital S" is still an S.
_LETTER_NOISE = frozenset({"capital", "uppercase", "lowercase", "small", "then", "and"})

_WORD = re.compile(r"[a-z]+(?:'[a-z]+)?")

#: A run of single capitals joined by hyphens -- the written spell-back.
WRITTEN_RUN = re.compile(r"\b[A-Z](?:-[A-Z])+\b")
#: The comma form, used when the name holds a character that is not a letter.
WRITTEN_COMMA_RUN = re.compile(r"\b[A-Z](?:, (?:[A-Z]|apostrophe|hyphen|space)\b)+")


# --------------------------------------------------------------------------
# Saying a name
# --------------------------------------------------------------------------


def spell_written(name: str) -> str:
    """A name as it appears in the transcript: "J-O-H-N".

    Hyphens join the letters, which reads cleanly on a screen -- until the name
    has a hyphen of its own, where "S-M-I-T-H-J-O-N-E-S" would hide it. A name
    with an apostrophe, a hyphen or a space is written with commas and the
    character named: "O, apostrophe, B, R, I, E, N".
    """
    characters = [c for c in name.strip() if c.isalpha() or c in SPECIAL_CHARACTERS]
    if any(c in SPECIAL_CHARACTERS for c in characters):
        return ", ".join(SPECIAL_CHARACTERS.get(c, c.upper()) for c in characters)
    return "-".join(c.upper() for c in characters)


#: Letters Aura runs into the one before, so a pause goes in front of them.
_VOWEL_LETTERS = frozenset("AEIOU")


def spell_for_speech(written: str) -> str:
    """The written spell-back, rewritten so a speech engine says each letter.

    "J-O-H-N" read as written is one word to a speech engine -- "john", or a
    noise. Commas between the letters make Aura say each one ("J, O, H, N"),
    and an ellipsis goes before every A, and before any vowel that follows a
    vowel ("B, I, L, Z, E, R, I... A, N").

    Measured, not assumed (2026-10-02). Candidates were synthesised by Aura,
    transcribed back with word timings, and repeated, because Aura does not
    say the same text the same way twice:

    * full stops dropped letters ("H. A. N. N. A. N" came back "h a n") and
      cut "A" to a 160 ms "uh" -- a caller heard it skipped;
    * commas fixed most of it, 20 of 24 complete, but still swallowed a
      vowel after a vowel: "I, A, N" ran together as "yan", and a caller
      spelling Bilzerian told the agent it had missed the A;
    * the pause before A and vowel-after-vowel letters: 53 of 54 complete
      across Bilzerian, Isaiah, Noah, Maria, Diana, Hannan, Ian, Leah, Adam
      and John. Pauses before *every* vowel, or on both sides of A, did worse.
    """
    parts = [part.strip() for part in re.split(r"-|, ", written) if part.strip()]
    if not parts:
        return ""
    spoken = parts[0]
    for previous, part in pairwise(parts):
        runs_together = part == "A" or (part in _VOWEL_LETTERS and previous in _VOWEL_LETTERS)
        spoken += ("... " if runs_together else ", ") + part
    return spoken


# --------------------------------------------------------------------------
# Reading a spelling
# --------------------------------------------------------------------------


def parse_spelled(text: str, *, allow_word: bool = False, min_letters: int = 2) -> str | None:
    """The letters a caller spelled, or ``None`` if this was not a spelling.

    Returned upper-case, with any apostrophe, hyphen or space they named:
    "O'BRIEN". ``None`` whenever anything in the utterance is neither a letter
    nor a word that goes around one -- "it's Jon without the h" is a
    correction, not a spelling, and reading letters out of it would invent a
    name. That case is the caller's to spell, and the agent asks.

    ``allow_word`` accepts a single ordinary word as the letters run together.
    Only when the agent has just asked for a spelling: the recogniser
    sometimes merges what was spelled into the word it spells ("SMYTH"), and
    in answer to "could you spell that?" a word *is* the spelling.

    ``min_letters`` is lowered to one only where a spelling is already under
    way: a lone "n" is the end of "H-A-N … N-A-N" split across two turns, and
    nowhere else is one letter a name.
    """
    lowered = text.lower().replace("\u2019", "'")
    # Letters glued together by the recogniser: "s-m-y-t-h", "s.m.y.t.h."
    lowered = re.sub(r"(?<=\b[a-z])[-.](?=[a-z]\b)", " ", lowered)
    lowered = re.sub(r"(?<=\b[a-z])\.", " ", lowered)
    lowered = lowered.replace("x-ray", "xray").replace("double-u", "double u")
    words: list[str] = _WORD.findall(lowered)

    start = 0
    while (
        start < len(words) and words[start] in _LEAD_INS and not _is_letter_run_start(words, start)
    ):
        start += 1
    end = len(words)
    while end > start and words[end - 1] in _TRAILERS and not _ends_a_spelling(words, end):
        end -= 1
    body = words[start:end]
    if not body:
        return None

    letters = _letters(body)
    if letters is not None and sum(c.isalpha() for c in letters) >= min_letters:
        # "Oh" alone is a hesitation, not the letter O.
        if letters == "O" and body == ["oh"]:
            return None
        return letters
    if allow_word and len(body) == 1 and len(body[0]) >= 2:
        return body[0].upper()
    return None


def _is_letter_run_start(words: list[str], index: int) -> bool:
    """Whether a lead-in word is really the first letter of the spelling.

    Only "be" is both: "B E R G" arrives as "be e r g". A lead-in that is
    followed by nothing but letters is taken as a letter.
    """
    if words[index] not in LETTER_WORDS:
        return False
    return _letters(words[index:]) is not None


def _ends_a_spelling(words: list[str], end: int) -> bool:
    """ "you" at the end is the letter U unless it follows "thank"."""
    word = words[end - 1]
    if word == "you" and end >= 2 and words[end - 2] == "thank":
        return False
    return word in LETTER_WORDS


def _letters(words: list[str]) -> str | None:
    """Every word a letter (or a named character), or ``None``."""
    out: list[str] = []
    index = 0
    while index < len(words):
        word = words[index]
        if word in _LETTER_NOISE:
            index += 1
            continue
        if word == "double" and index + 1 < len(words) and words[index + 1] in {"you", "u", "yew"}:
            # The letter W, not two Us: nobody's name has a double U in it,
            # and "double you" is how W is said.
            out.append("W")
            index += 2
            continue
        if word == "double" and index + 1 < len(words):
            letter = _letter(words[index + 1])
            if letter is None:
                return None
            out.append(letter * 2)
            index += 2
            continue
        if word in _SPOKEN_SPECIALS:
            out.append(_SPOKEN_SPECIALS[word])
            index += 1
            continue
        letter = _letter(word)
        if letter is None:
            return None
        out.append(letter)
        index += 1
        index = _skip_example(words, index)
    if not out:
        return None
    return "".join(out)


def _letter(word: str) -> str | None:
    if len(word) == 1 and word.isalpha():
        return word.upper()
    return LETTER_WORDS.get(word) or NATO_ALPHABET.get(word)


def _skip_example(words: list[str], index: int) -> int:
    """Step over "as in Sam" after a letter; the example is not a letter."""
    for lead in _AS_IN:
        span = len(lead)
        if tuple(words[index : index + span]) == lead and index + span < len(words):
            return index + span + 1
    return index


#: What a spelling or a number read aloud can stop on while still unfinished.
_NUMBER_TOKENS = frozenset(
    {
        "zero",
        "oh",
        "one",
        "two",
        "three",
        "four",
        "five",
        "six",
        "seven",
        "eight",
        "nine",
        "ten",
        "eleven",
        "twelve",
        "thirteen",
        "fourteen",
        "fifteen",
        "sixteen",
        "seventeen",
        "eighteen",
        "nineteen",
        "twenty",
        "thirty",
        "forty",
        "fifty",
        "sixty",
        "seventy",
        "eighty",
        "ninety",
        "hundred",
        "thousand",
        "and",
        "first",
        "second",
        "third",
        "fourth",
        "fifth",
        "sixth",
        "seventh",
        "eighth",
        "ninth",
        "tenth",
        "eleventh",
        "twelfth",
        "thirteenth",
        "fourteenth",
        "fifteenth",
        "sixteenth",
        "seventeenth",
        "eighteenth",
        "nineteenth",
        "twentieth",
        "thirtieth",
        "of",
        "the",
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
        "hyphen",
        "dash",
        "apostrophe",
        "space",
        "double",
        "as",
        "in",
        "for",
    }
)


def sounds_unfinished(text: str) -> bool:
    """Whether dictation stopped on something that is rarely the last of it.

    A letter, a digit, a number word, a month. Callers pause between letters
    and between the parts of a date, and the recogniser calls each pause the
    end of the utterance: on a live call "H-A-N-N-A-N" arrived as "H a n" and
    then "n", and the agent answered the first half -- spelling back a
    three-letter surname, then a one-letter one. Ending on "yes" or a name is
    finished; ending on "n" or "nineteen" probably is not.
    """
    words = re.findall(r"[a-z0-9']+", text.lower())
    if not words:
        return False
    last = words[-1]
    return (
        len(last) == 1
        or last.isdigit()
        or last in _NUMBER_TOKENS
        or last in LETTER_WORDS
        or last in NATO_ALPHABET
    )


# --------------------------------------------------------------------------
# Name parts
# --------------------------------------------------------------------------

#: Lower-case words that belong to the surname after them: "van der Berg",
#: "de la Cruz". Without this, "Maria van der Berg" has a middle name "van der".
_SURNAME_PARTICLES = frozenset(
    {"van", "von", "der", "den", "de", "la", "le", "del", "della", "da", "di", "dos", "du", "st"}
)


@dataclass(frozen=True)
class NameParts:
    given: str
    family: str | None = None
    middle: str | None = None

    @property
    def full(self) -> str:
        return " ".join(part for part in (self.given, self.middle, self.family) if part)

    def as_dict(self) -> dict[str, str | None]:
        return {"given": self.given, "middle": self.middle, "family": self.family}

    @classmethod
    def from_dict(cls, raw: object) -> NameParts | None:
        if not isinstance(raw, dict) or not isinstance(raw.get("given"), str):
            return None
        family, middle = raw.get("family"), raw.get("middle")
        return cls(
            given=raw["given"],
            family=family if isinstance(family, str) else None,
            middle=middle if isinstance(middle, str) else None,
        )


def tidy_name(text: str) -> str:
    """Capitalised the way a record holds it: "o'brien" -> "O'Brien".

    Transcripts arrive in lower case as often as not, and a name taken down in
    lower case is spelled back in capitals and stored capitalised either way.
    """
    words = " ".join(text.split())
    return re.sub(r"[A-Za-z]+", lambda m: m.group(0)[0].upper() + m.group(0)[1:].lower(), words)


def split_name(full_name: str) -> NameParts:
    """Given, middle and family name, from a name said in one go."""
    words = tidy_name(full_name).split()
    if len(words) == 1:
        return NameParts(given=words[0])
    family_start = len(words) - 1
    while family_start > 1 and words[family_start - 1].lower() in _SURNAME_PARTICLES:
        family_start -= 1
    middle = " ".join(words[1:family_start]) or None
    return NameParts(given=words[0], middle=middle, family=" ".join(words[family_start:]))


def name_from_letters(letters: str) -> str:
    """ "O'BRIEN" -> "O'Brien"."""
    return tidy_name(letters)
