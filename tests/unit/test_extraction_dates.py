"""Dates that arrive as speech.

Speech recognition returns what was said, and people say dates out loud. Every
date-of-birth pattern in this system required digits, so a voice caller could
never be verified at all -- which is the one place where failing to parse means
failing to identify a real patient.

Deepgram's `smart_format` is deliberately not used to solve this: it emits US
ordering ("03/04/1978"), which is indistinguishable from 3 April to a parser
that has to guess, and it mangles bare years. The spoken form is unambiguous,
so it is kept and resolved here.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.agents.extraction import RuleBasedExtractor, _spoken_numbers_to_digits


class TestSpokenDates:
    @pytest.mark.parametrize(
        ("spoken", "expected"),
        [
            ("I was born on the fifteenth of February nineteen eighty five", date(1985, 2, 15)),
            ("the fourteenth of March nineteen seventy eight", date(1978, 3, 14)),
            ("February fifteenth nineteen eighty five", date(1985, 2, 15)),
            ("twenty first December two thousand two", date(2002, 12, 21)),
            ("my date of birth is the third of June two thousand and one", date(2001, 6, 3)),
            ("first of January nineteen ninety", date(1990, 1, 1)),
            ("thirtieth of April two thousand and ten", date(2010, 4, 30)),
        ],
        ids=[
            "day-month-year",
            "day-month-year-alt",
            "month-day-year",
            "compound-ordinal",
            "thousand-and-unit",
            "bare-ordinal-and-round-year",
            "tens-ordinal",
        ],
    )
    def test_spoken_dates_parse(self, spoken: str, expected: date) -> None:
        assert RuleBasedExtractor._date_of_birth(spoken) == expected

    @pytest.mark.parametrize(
        "written",
        ["15 February 1985", "1985-02-15", "February 15, 1985", "15/02/1985"],
    )
    def test_written_dates_still_parse(self, written: str) -> None:
        """The digit paths must not regress: text mode is the primary surface."""
        assert RuleBasedExtractor._date_of_birth(written) == date(1985, 2, 15)

    @pytest.mark.parametrize(
        "utterance",
        [
            "I have three children",
            "I'd like to book an appointment",
            "My name is John Smith",
            "Can I take the first one please",
            "",
        ],
    )
    def test_utterances_without_a_date_yield_none(self, utterance: str) -> None:
        """A number-word rewriter that is too eager starts inventing dates."""
        assert RuleBasedExtractor._date_of_birth(utterance) is None

    def test_number_words_become_digits_without_losing_the_words_between(self) -> None:
        assert (
            _spoken_numbers_to_digits(
                "I was born on the fifteenth of February nineteen eighty five"
            )
            == "i was born on 15 february 1985"
        )

    def test_years_said_as_pairs(self) -> None:
        """ "nineteen eighty five" is 1985, not 19 and 85."""
        assert _spoken_numbers_to_digits("nineteen eighty five") == "1985"
        assert _spoken_numbers_to_digits("nineteen seventy") == "1970"
        assert _spoken_numbers_to_digits("two thousand and two") == "2002"
        assert _spoken_numbers_to_digits("two thousand") == "2000"
