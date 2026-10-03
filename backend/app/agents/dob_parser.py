"""Reading a date of birth out of what a caller said.

A date of birth is the one value in this system where a wrong parse is worse
than no parse. A wrong name fails verification and is asked again; a wrong
date that happens to be *valid* verifies nobody, burns one of three attempts,
and -- during registration -- is written into a new record. So this module
never guesses. It finds every reading of the utterance that makes a real date,
and reports how many there were:

* **one** -- CONFIDENT, and the agent still reads it back;
* **two** -- AMBIGUOUS, and the agent asks which ("March fourth or April
  third?") rather than picking;
* **none, or more than two** -- NONE, and the agent asks again with a hint.

The old path ran ``dateutil`` with ``dayfirst=True``. That turned "03/04/1990"
into 3 April without a word -- for a US clinic, where the caller almost
certainly meant 4 March -- and it parsed "the 15th of October 2026" as a date of
birth in the future. Month-first is the default now, and when month-first and
day-first both make sense the caller is asked, because only they know.

**How.** The utterance becomes a sequence of numbers and at most one month
word. Spoken numbers are segmented every way they can be ("fifteen eighty
five" is 1585, or 15 and 85, or 15, 80 and 5), and each segmentation is kept
only if *every* number in it fits a date shape. That constraint does almost
all the work: "Jan fifteen eighty-five" has exactly one reading that uses all
its numbers -- 15 January 1985 -- so it is confident without any heuristic
deciding that 1585 is unlikely.

Pure functions, no I/O, no clock: ``today`` is passed in, so the two-digit-year
pivot and the "not in the future" rule are testable forever.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from itertools import product

#: Oldest plausible caller. Anything older is a mis-hearing -- "1885" for 1985.
MAX_AGE_YEARS = 120

#: Beyond this many readings the parser stops enumerating. Real utterances
#: produce a handful; the cap exists so a long run of number words cannot make
#: a turn slow.
_MAX_READINGS = 512


class DobStatus(StrEnum):
    CONFIDENT = "CONFIDENT"
    AMBIGUOUS = "AMBIGUOUS"
    NONE = "NONE"


@dataclass(frozen=True)
class DobParse:
    """Every real date the utterance could mean, and how sure that makes us."""

    status: DobStatus
    candidates: tuple[date, ...] = ()

    @property
    def value(self) -> date | None:
        """The date, when there is exactly one."""
        return self.candidates[0] if self.status is DobStatus.CONFIDENT else None


NO_DATE = DobParse(DobStatus.NONE)


# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

_MONTHS: dict[str, int] = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sept": 9,
    "sep": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}

MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

#: Single digits, as read out one at a time. "Oh" is a zero only inside a run
#: of them -- "oh one one five" -- and an ordinary noise anywhere else.
_DIGIT_WORDS: dict[str, int] = {
    "zero": 0,
    "oh": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
}
_TEENS: dict[str, int] = {
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
}
_ORDINAL_UNITS: dict[str, int] = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
}
_ORDINAL_TEENS: dict[str, int] = {
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
_ORDINAL_TENS: dict[str, int] = {"twentieth": 20, "thirtieth": 30}

_NUMBER_WORDS = frozenset(
    {
        *_DIGIT_WORDS,
        *_TEENS,
        *_TENS,
        *_ORDINAL_UNITS,
        *_ORDINAL_TEENS,
        *_ORDINAL_TENS,
        "hundred",
        "thousand",
    }
)

#: Noises that sit inside a date without being part of it. Removed before the
#: number words are grouped, so "nineteen, um, eighty five" is still a year
#: rather than 19 and 85.
_FILLERS = frozenset({"um", "uh", "er", "erm", "uhm", "hmm", "like", "so"})

_TOKEN = re.compile(r"(\d+)(st|nd|rd|th)?|([a-z]+)|([/\-.])|(,)")


# --------------------------------------------------------------------------
# Numbers
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Num:
    """One number in the utterance, with what it can and cannot be."""

    value: int
    #: As written or read out, leading zeros kept: "0115" is not 115.
    digits: str
    #: "Fifteenth", "15th". Only ever a day.
    ordinal: bool = False
    #: "Nineteen eighty five". Only ever a year.
    year: bool = False
    #: Digits given as digits -- written, or read out one at a time. Only these
    #: can be a month and day run together ("0115"); "nineteen" said as a
    #: word is a number, not the digits one and nine.
    sequence: bool = False


def _num(value: int, *, ordinal: bool = False, year: bool = False) -> _Num:
    return _Num(value, str(value), ordinal=ordinal, year=year)


def _segmentations(words: list[str]) -> list[list[_Num]]:
    """Every way a run of number words can be read as a list of numbers."""
    memo: dict[int, list[list[_Num]]] = {}

    def from_(index: int) -> list[list[_Num]]:
        if index == len(words):
            return [[]]
        if index in memo:
            return memo[index]
        readings: list[list[_Num]] = []
        for atom, after in _productions(words, index):
            for rest in from_(after):
                readings.append(([atom] if atom is not None else []) + rest)
                if len(readings) >= _MAX_READINGS:
                    break
        memo[index] = readings
        return readings

    return from_(0)


def _productions(words: list[str], i: int) -> list[tuple[_Num | None, int]]:
    """The numbers that can start at ``words[i]``, and where each one ends.

    A dead end (no production) is how an impossible reading is discarded:
    "hundred" on its own starts nothing, so any segmentation that leaves one
    stranded never completes.
    """
    word = words[i]
    nxt = words[i + 1] if i + 1 < len(words) else ""
    out: list[tuple[_Num | None, int]] = []

    # "two thousand", "two thousand and two", "two thousand twenty one"
    if word in _DIGIT_WORDS and word not in {"zero", "oh"} and nxt == "thousand":
        base = _DIGIT_WORDS[word] * 1000
        out.append((_num(base, year=True), i + 2))
        j = i + 2
        if j < len(words) and words[j] == "and":
            j += 1
        for tail, end in _tails_below_a_hundred(words, j):
            out.append((_num(base + tail, year=True), end))
        return out

    # A run of digits read one at a time: "zero one one five".
    if word in _DIGIT_WORDS:
        j = i
        while j < len(words) and words[j] in _DIGIT_WORDS:
            j += 1
        run = words[i:j]
        if run == ["oh"]:
            # A lone "oh" is a noise, not a zero.
            out.append((None, j))
        else:
            out.append((_Num(int(_digits(run)), _digits(run), sequence=True), j))
        return out

    # "nineteen eighty five", "twenty oh three", "twenty twenty", "nineteen hundred"
    if word in _TEENS or word == "twenty":
        century = (_TEENS.get(word) or _TENS[word]) * 100
        if nxt == "hundred":
            out.append((_num(century, year=True), i + 2))
        if nxt in {"oh", "zero"} and i + 2 < len(words):
            unit = words[i + 2]
            if unit in _DIGIT_WORDS and _DIGIT_WORDS[unit] > 0:
                out.append((_num(century + _DIGIT_WORDS[unit], year=True), i + 3))
        if nxt in _TEENS:
            out.append((_num(century + _TEENS[nxt], year=True), i + 2))
        if nxt in _TENS:
            out.append((_num(century + _TENS[nxt], year=True), i + 2))
            if i + 2 < len(words):
                unit = words[i + 2]
                if unit in _DIGIT_WORDS and _DIGIT_WORDS[unit] > 0:
                    out.append((_num(century + _TENS[nxt] + _DIGIT_WORDS[unit], year=True), i + 3))

    if word in _TEENS:
        out.append((_num(_TEENS[word]), i + 1))
    elif word in _TENS:
        out.append((_num(_TENS[word]), i + 1))
        if nxt in _DIGIT_WORDS and _DIGIT_WORDS[nxt] > 0:
            out.append((_num(_TENS[word] + _DIGIT_WORDS[nxt]), i + 2))
        if nxt in _ORDINAL_UNITS:
            out.append((_num(_TENS[word] + _ORDINAL_UNITS[nxt], ordinal=True), i + 2))
    elif word in _ORDINAL_UNITS:
        out.append((_num(_ORDINAL_UNITS[word], ordinal=True), i + 1))
    elif word in _ORDINAL_TEENS:
        out.append((_num(_ORDINAL_TEENS[word], ordinal=True), i + 1))
    elif word in _ORDINAL_TENS:
        out.append((_num(_ORDINAL_TENS[word], ordinal=True), i + 1))
    return out


def _tails_below_a_hundred(words: list[str], j: int) -> list[tuple[int, int]]:
    """What can follow "two thousand": a unit, a teen, or tens and a unit."""
    if j >= len(words):
        return []
    word = words[j]
    nxt = words[j + 1] if j + 1 < len(words) else ""
    if word in _DIGIT_WORDS and _DIGIT_WORDS[word] > 0 and word != "oh":
        return [(_DIGIT_WORDS[word], j + 1)]
    if word in _TEENS:
        return [(_TEENS[word], j + 1)]
    if word in _TENS:
        tails = [(_TENS[word], j + 1)]
        if nxt in _DIGIT_WORDS and _DIGIT_WORDS[nxt] > 0:
            tails.append((_TENS[word] + _DIGIT_WORDS[nxt], j + 2))
        return tails
    return []


def _digits(run: list[str]) -> str:
    return "".join(str(_DIGIT_WORDS[w]) for w in run)


def _merged_years(nums: list[_Num]) -> list[list[_Num]]:
    """Readings in which written digit groups are pieces of one year.

    Deepgram's formatter writes "nineteen seventy eight" as "19 70 8" -- seen in
    testing, and the reason the spoken path exists at all. "19 85" is the same
    artefact one step short. Offered as alternatives rather than applied, so
    "1 19 85" can still be the 19th of January.
    """
    readings: list[list[_Num]] = [nums]
    for i, first in enumerate(nums):
        if first.digits not in {"19", "20"} or first.ordinal or i + 1 >= len(nums):
            continue
        second = nums[i + 1]
        if len(second.digits) != 2 or second.ordinal:
            continue
        merged = _num(int(first.digits + second.digits), year=True)
        readings.append([*nums[:i], merged, *nums[i + 2 :]])
        if i + 2 < len(nums) and second.digits.endswith("0"):
            third = nums[i + 2]
            if len(third.digits) == 1 and not third.ordinal:
                year = int(first.digits) * 100 + int(second.digits) + int(third.digits)
                readings.append([*nums[:i], _num(year, year=True), *nums[i + 3 :]])
    return readings


# --------------------------------------------------------------------------
# Tokenising
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Reading:
    month: int | None
    alternatives: list[list[_Num]]


def _read(text: str) -> _Reading | None:
    """The month word, if any, and every way of reading the numbers.

    ``None`` when the utterance cannot be a date at all -- two month words, for
    instance, which is somebody describing a range rather than a birthday.
    """
    lowered = text.lower().replace("\u2019", "'")
    month: int | None = None
    # Each element is either fixed numbers (written digits) or the choices for
    # one run of number words.
    pieces: list[list[list[_Num]]] = []
    run: list[str] = []

    def close_run() -> None:
        if not run:
            return
        # "and" is only glue inside "two thousand and two"; trailing, it is
        # the start of the next clause.
        while run and run[-1] == "and":
            run.pop()
        if run:
            choices = _segmentations(list(run))
            pieces.append(choices if choices else [])
        run.clear()

    tokens = _TOKEN.findall(lowered)
    for index, (digits, suffix, word, _separator, _comma) in enumerate(tokens):
        if digits:
            close_run()
            pieces.append([[_Num(int(digits), digits, ordinal=bool(suffix), sequence=not suffix)]])
            continue
        if not word:
            # A hyphen between two words is part of a number ("eighty-five");
            # every other separator ends the run.
            if _separator == "-" and run:
                continue
            close_run()
            continue
        if word in _FILLERS:
            continue
        if word in _NUMBER_WORDS:
            run.append(word)
            continue
        if word == "and" and run and _next_is_number(tokens, index):
            run.append(word)
            continue
        close_run()
        if word in _MONTHS:
            if month is not None and month != _MONTHS[word]:
                return None
            month = _MONTHS[word]
    close_run()

    if any(not choices for choices in pieces):
        # A run of number words with no complete reading -- "hundred" alone.
        return None

    alternatives: list[list[_Num]] = []
    for combination in product(*pieces):
        flat = [num for part in combination for num in part]
        alternatives.extend(_merged_years(flat))
        if len(alternatives) >= _MAX_READINGS:
            break
    return _Reading(month=month, alternatives=alternatives)


def _next_is_number(tokens: list[tuple[str, str, str, str, str]], index: int) -> bool:
    for digits, _suffix, word, _sep, _comma in tokens[index + 1 :]:
        if word in _FILLERS:
            continue
        return bool(word) and word in _NUMBER_WORDS and not digits
    return False


# --------------------------------------------------------------------------
# Shapes
# --------------------------------------------------------------------------

#: A candidate before the century is settled: (year, year was two digits,
#: month, day).
_Shape = tuple[int, bool, int, int]


def _as_day(num: _Num) -> int | None:
    if num.year or len(num.digits) > 2:
        return None
    return num.value if 1 <= num.value <= 31 else None


def _as_month(num: _Num) -> int | None:
    if num.year or num.ordinal or len(num.digits) > 2:
        return None
    return num.value if 1 <= num.value <= 12 else None


def _as_year(num: _Num) -> tuple[int, bool] | None:
    if num.ordinal:
        return None
    if len(num.digits) == 4:
        return num.value, False
    if len(num.digits) == 2 and not num.year:
        return num.value, True
    return None


def _shapes_with_month(month: int, nums: list[_Num]) -> list[_Shape]:
    """A month was named, so the numbers are a day and a year, in either order."""
    if len(nums) != 2:
        return []
    shapes: list[_Shape] = []
    for day_num, year_num in ((nums[0], nums[1]), (nums[1], nums[0])):
        day, year = _as_day(day_num), _as_year(year_num)
        if day is not None and year is not None:
            shapes.append((year[0], year[1], month, day))
    return shapes


def _shapes_without_month(nums: list[_Num]) -> list[_Shape]:
    if len(nums) == 1:
        return _compact(nums[0])
    if len(nums) == 2:
        return _month_day_then_year(nums[0], nums[1])
    if len(nums) == 3:
        return _three_parts(*nums)
    return []


def _three_parts(a: _Num, b: _Num, c: _Num) -> list[_Shape]:
    """Separated digits: month/day/year by default, day/month/year, or ISO."""
    shapes: list[_Shape] = []
    iso_year = _as_year(a)
    if iso_year is not None and not iso_year[1]:
        month, day = _as_month(b), _as_day(c)
        if month is not None and day is not None:
            shapes.append((iso_year[0], False, month, day))
    year = _as_year(c)
    if year is not None:
        for month_num, day_num in ((a, b), (b, a)):
            month, day = _as_month(month_num), _as_day(day_num)
            if month is not None and day is not None:
                shapes.append((year[0], year[1], month, day))
    return shapes


def _month_day_then_year(first: _Num, second: _Num) -> list[_Shape]:
    """ "Oh one one five, nineteen eighty five": month and day said as one group."""
    year = _as_year(second)
    if year is None or not first.sequence:
        return []
    splits = {2: ((1, 1),), 3: ((1, 2), (2, 1)), 4: ((2, 2),)}.get(len(first.digits), ())
    shapes: list[_Shape] = []
    for left, right in splits:
        head, tail = first.digits[:left], first.digits[left : left + right]
        for month_text, day_text in ((head, tail), (tail, head)):
            month, day = int(month_text), int(day_text)
            if 1 <= month <= 12 and 1 <= day <= 31:
                shapes.append((year[0], year[1], month, day))
    return shapes


#: Compact digit strings, by length: (field order, field widths).
_COMPACT: dict[int, tuple[tuple[str, tuple[int, int, int]], ...]] = {
    8: (("mdy", (2, 2, 4)), ("dmy", (2, 2, 4)), ("ymd", (4, 2, 2))),
    7: (("mdy", (1, 2, 4)), ("mdy", (2, 1, 4)), ("dmy", (1, 2, 4)), ("dmy", (2, 1, 4))),
    6: (("mdy", (2, 2, 2)), ("dmy", (2, 2, 2))),
}


def _compact(num: _Num) -> list[_Shape]:
    """ "01151985", "19850115", "011585": every layout, kept if it is a date.

    Lengths outside the table are refused outright. "00010019197" -- eleven
    digits, recorded from a caller who was reading a date and lost their place
    -- has no honest reading, and a parser that found one would be guessing.
    """
    if not num.sequence:
        return []
    shapes: list[_Shape] = []
    for order, widths in _COMPACT.get(len(num.digits), ()):
        fields: dict[str, str] = {}
        position = 0
        for name, width in zip(order, widths, strict=True):
            fields[name] = num.digits[position : position + width]
            position += width
        month, day = int(fields["m"]), int(fields["d"])
        if 1 <= month <= 12 and 1 <= day <= 31:
            shapes.append((int(fields["y"]), len(fields["y"]) == 2, month, day))
    return shapes


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def _resolve(shape: _Shape, today: date) -> date | None:
    """A real date, inside a lifetime, or nothing.

    A two-digit year takes whichever century keeps it out of the future: "05"
    is 2005 once 2005 has happened, and "85" can only be 1985.
    """
    year, two_digit, month, day = shape
    if two_digit:
        for century in (2000, 1900):
            resolved = _valid(century + year, month, day, today)
            if resolved is not None:
                return resolved
        return None
    return _valid(year, month, day, today)


def _valid(year: int, month: int, day: int, today: date) -> date | None:
    try:
        candidate = date(year, month, day)
    except ValueError:
        # 30 February; 29 February outside a leap year.
        return None
    if candidate > today:
        return None
    if _years_between(candidate, today) > MAX_AGE_YEARS:
        return None
    return candidate


def _years_between(earlier: date, later: date) -> int:
    years = later.year - earlier.year
    if (later.month, later.day) < (earlier.month, earlier.day):
        years -= 1
    return years


# --------------------------------------------------------------------------
# Public
# --------------------------------------------------------------------------


def parse_date_of_birth(text: str, today: date) -> DobParse:
    """Every real date of birth ``text`` could mean.

    Never picks between readings: two survivors are AMBIGUOUS, and deciding
    between them is a question for the caller.
    """
    reading = _read(text)
    if reading is None:
        return NO_DATE

    found: list[date] = []
    for nums in reading.alternatives:
        shapes = (
            _shapes_with_month(reading.month, nums)
            if reading.month is not None
            else _shapes_without_month(nums)
        )
        for shape in shapes:
            resolved = _resolve(shape, today)
            if resolved is not None and resolved not in found:
                found.append(resolved)

    if not found:
        return NO_DATE
    if len(found) == 1:
        return DobParse(DobStatus.CONFIDENT, (found[0],))
    return DobParse(DobStatus.AMBIGUOUS, tuple(sorted(found)))


# --------------------------------------------------------------------------
# Saying a date back
# --------------------------------------------------------------------------

_ORDINAL_WORDS = (
    "",
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
)
_CARDINAL_UNITS = (
    "",
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
)
_CARDINAL_TENS = (
    "",
    "",
    "twenty",
    "thirty",
    "forty",
    "fifty",
    "sixty",
    "seventy",
    "eighty",
    "ninety",
)


def ordinal_day(day: int) -> str:
    """15 -> "fifteenth"; 21 -> "twenty-first"."""
    if day <= 20:
        return _ORDINAL_WORDS[day]
    tens, unit = divmod(day, 10)
    if unit == 0:
        return "thirtieth"
    return f"{_CARDINAL_TENS[tens]}-{_ORDINAL_WORDS[unit]}"


def _below_a_hundred(value: int) -> str:
    if value < 20:
        return _CARDINAL_UNITS[value]
    tens, unit = divmod(value, 10)
    return _CARDINAL_TENS[tens] + (f"-{_CARDINAL_UNITS[unit]}" if unit else "")


def spoken_year(year: int) -> str:
    """The way people say a year: "nineteen eighty-five", "two thousand and four"."""
    if 2000 <= year <= 2009:
        return "two thousand" + (f" and {_CARDINAL_UNITS[year - 2000]}" if year > 2000 else "")
    century, rest = divmod(year, 100)
    head = _below_a_hundred(century)
    if rest == 0:
        return f"{head} hundred"
    if rest < 10:
        return f"{head} oh {_CARDINAL_UNITS[rest]}"
    return f"{head} {_below_a_hundred(rest)}"


def speak_date(value: date) -> str:
    """A date read back so it cannot be misheard: the month is always a word.

    "The fifteenth of January, nineteen eighty-five." Digits are exactly what
    made the date ambiguous in the first place, so the read-back never uses
    them -- a caller who said "oh one one five" hears which month that was.
    """
    return (
        f"the {ordinal_day(value.day)} of {MONTH_NAMES[value.month - 1]}, {spoken_year(value.year)}"
    )


def speak_month_day(value: date) -> str:
    """ "March fourth" -- enough to tell two readings of one date apart."""
    return f"{MONTH_NAMES[value.month - 1]} {ordinal_day(value.day)}"
