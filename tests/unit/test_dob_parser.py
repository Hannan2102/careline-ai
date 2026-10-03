"""Reading a date of birth, and knowing when not to.

Every shape here is one a caller produces: out loud, through a recogniser, or
typed into the chat. The ambiguous cases matter as much as the confident ones
-- the old parser resolved "03/04/1990" to 3 April without a word, at a US
clinic where the caller almost certainly meant 4 March.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.agents.dob_parser import (
    DobStatus,
    parse_date_of_birth,
    speak_date,
    speak_month_day,
    spoken_year,
)

#: Fixed, so the two-digit pivot and "not in the future" are tested against a
#: date that cannot move.
TODAY = date(2026, 10, 1)


def confident(text: str, today: date = TODAY) -> date | None:
    parsed = parse_date_of_birth(text, today)
    assert parsed.status is DobStatus.CONFIDENT, f"{text!r} -> {parsed}"
    return parsed.value


class TestSpokenMonths:
    @pytest.mark.parametrize(
        "text",
        [
            "January 15th 1985",
            "15th of January 1985",
            "the fifteenth of Jan, nineteen eighty-five",
            "Jan fifteen eighty-five",
            "January fifteenth nineteen eighty five",
            "15 Jan 1985",
            "Jan. 15, 1985",
            "1985 January 15",
        ],
    )
    def test_month_names_in_any_order(self, text: str) -> None:
        assert confident(text) == date(1985, 1, 15)

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("15 Feb 1985", date(1985, 2, 15)),
            ("Sept 3 1990", date(1990, 9, 3)),
            ("3 Nov 1972", date(1972, 11, 3)),
            ("December 1st 1999", date(1999, 12, 1)),
        ],
    )
    def test_abbreviated_months(self, text: str, expected: date) -> None:
        assert confident(text) == expected


class TestSpokenYears:
    @pytest.mark.parametrize(
        ("year_words", "year"),
        [
            ("nineteen eighty-five", 1985),
            ("nineteen eighty five", 1985),
            ("eighty-five", 1985),
            ("two thousand", 2000),
            ("two thousand and two", 2002),
            ("two thousand two", 2002),
            ("twenty oh three", 2003),
            ("twenty twenty", 2020),
            ("nineteen oh nine", 1909),
            ("twenty fifteen", 2015),
        ],
    )
    def test_years_as_people_say_them(self, year_words: str, year: int) -> None:
        assert confident(f"March the fourth {year_words}") == date(year, 3, 4)

    def test_a_round_century(self) -> None:
        # Against an earlier "today": 1900 is more than 120 years before TODAY.
        assert confident("March 4th nineteen hundred", today=date(2010, 1, 1)) == date(1900, 3, 4)

    @pytest.mark.parametrize(
        ("day_words", "day"),
        [
            ("first", 1),
            ("twenty-first", 21),
            ("twenty first", 21),
            ("thirty", 30),
            ("thirtieth", 30),
            ("twelfth", 12),
            ("fifteen", 15),
        ],
    )
    def test_days_ordinal_and_cardinal(self, day_words: str, day: int) -> None:
        assert confident(f"January {day_words} nineteen ninety") == date(1990, 1, day)


class TestDigits:
    def test_month_first_is_the_default(self) -> None:
        """A US clinic: "1/15/85" can only be the fifteenth of January."""
        assert confident("1/15/85") == date(1985, 1, 15)
        assert confident("12-25-1990") == date(1990, 12, 25)

    def test_day_first_when_only_day_first_makes_a_date(self) -> None:
        assert confident("15/02/1985") == date(1985, 2, 15)
        assert confident("15.02.1985") == date(1985, 2, 15)

    def test_iso(self) -> None:
        assert confident("1985-02-15") == date(1985, 2, 15)

    def test_spaces_separate_like_slashes(self) -> None:
        assert confident("1 15 1985") == date(1985, 1, 15)

    def test_both_readings_the_same_is_not_ambiguous(self) -> None:
        assert confident("01/01/1970") == date(1970, 1, 1)

    @pytest.mark.parametrize(
        "text", ["03/04/1990", "3-4-1990", "zero three zero four ninety", "03041990"]
    )
    def test_day_and_month_both_twelve_or_under_is_asked_not_guessed(self, text: str) -> None:
        parsed = parse_date_of_birth(text, TODAY)
        assert parsed.status is DobStatus.AMBIGUOUS
        assert parsed.candidates == (date(1990, 3, 4), date(1990, 4, 3))
        assert parsed.value is None


class TestTwoDigitYears:
    def test_the_century_that_keeps_it_in_the_past(self) -> None:
        assert confident("1/15/05") == date(2005, 1, 15)
        assert confident("1/15/85") == date(1985, 1, 15)

    def test_this_years_two_digits_depend_on_whether_the_day_has_passed(self) -> None:
        # 2026-01-15 has happened; 2026-12-15 has not, so it must be 1926.
        assert confident("1/15/26") == date(2026, 1, 15)
        assert confident("12/15/26") == date(1926, 12, 15)

    def test_the_pivot_moves_with_today(self) -> None:
        assert confident("1/15/30", today=date(2031, 1, 1)) == date(2030, 1, 15)
        assert confident("1/15/30", today=TODAY) == date(1930, 1, 15)


class TestCompactDigits:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("01151985", date(1985, 1, 15)),  # MMDDYYYY
            ("15021985", date(1985, 2, 15)),  # DDMMYYYY, MM reading impossible
            ("19850115", date(1985, 1, 15)),  # YYYYMMDD
            ("011585", date(1985, 1, 15)),  # MMDDYY
        ],
    )
    def test_every_layout_is_tried_and_only_real_dates_survive(
        self, text: str, expected: date
    ) -> None:
        assert confident(text) == expected

    @pytest.mark.parametrize("text", ["00010019197", "1985", "12345", "0411"])
    def test_garbled_or_wrong_length_is_never_a_guess(self, text: str) -> None:
        """ "00010019197" was transcribed from a caller who lost their place."""
        assert parse_date_of_birth(text, TODAY).status is not DobStatus.CONFIDENT


class TestDigitsReadOneAtATime:
    def test_all_spoken_digits(self) -> None:
        assert confident("zero one one five one nine eight five") == date(1985, 1, 15)

    def test_oh_as_zero_then_a_spoken_year(self) -> None:
        assert confident("oh one one five nineteen eighty-five") == date(1985, 1, 15)

    def test_mixed_digits_and_words(self) -> None:
        assert confident("01 15 nineteen eighty five") == date(1985, 1, 15)
        assert confident("January 15 nineteen eighty five") == date(1985, 1, 15)

    def test_a_lone_oh_is_a_noise(self) -> None:
        assert confident("oh, January fifteenth, 1985") == date(1985, 1, 15)


class TestRecogniserArtefacts:
    def test_a_year_split_into_pieces(self) -> None:
        """Deepgram wrote "nineteen seventy eight" as "19 70 8" in testing."""
        assert confident("the 14th of March 19 70 8") == date(1978, 3, 14)
        assert confident("March 14 19 78") == date(1978, 3, 14)

    @pytest.mark.parametrize(
        "text",
        [
            "um, I was born on the fifteenth of February, uh, nineteen eighty five.",
            "It's like February 15th 1985!",
            "My date of birth is 15 Feb 1985.",
            "nineteen, um, eighty five, February fifteenth",
        ],
    )
    def test_fillers_and_punctuation(self, text: str) -> None:
        assert confident(text) == date(1985, 2, 15)


class TestValidation:
    @pytest.mark.parametrize(
        "text",
        [
            "February 30th 1990",  # no such day
            "February 29th 2023",  # not a leap year
            "October 15th 2026",  # after today
            "January 1st 1890",  # more than 120 years ago
            "13/13/1990",
        ],
    )
    def test_impossible_dates_are_no_date(self, text: str) -> None:
        assert parse_date_of_birth(text, TODAY).status is DobStatus.NONE

    def test_a_leap_day_in_a_leap_year(self) -> None:
        assert confident("February 29th 2000") == date(2000, 2, 29)

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "I'd like to book an appointment",
            "My name is John Smith",
            "Can I take the first one please",
            "I have three children",
            "nineteen eighty five",  # a year is not a date of birth
            "March 4th",  # nor is a day without one
            "555 019 0142",
            "January and February 1985",
        ],
    )
    def test_no_date_where_there_is_none(self, text: str) -> None:
        assert parse_date_of_birth(text, TODAY).status is DobStatus.NONE


class TestSpeakingADate:
    @pytest.mark.parametrize(
        ("value", "spoken"),
        [
            (date(1985, 1, 15), "the fifteenth of January, nineteen eighty-five"),
            (date(2004, 3, 2), "the second of March, two thousand and four"),
            (date(2000, 12, 21), "the twenty-first of December, two thousand"),
            (date(1905, 6, 30), "the thirtieth of June, nineteen oh five"),
            (date(2015, 7, 4), "the fourth of July, twenty fifteen"),
            (date(1900, 5, 1), "the first of May, nineteen hundred"),
        ],
    )
    def test_the_read_back_never_uses_digits(self, value: date, spoken: str) -> None:
        assert speak_date(value) == spoken

    def test_month_and_day_for_telling_two_readings_apart(self) -> None:
        assert speak_month_day(date(1990, 3, 4)) == "March fourth"
        assert speak_month_day(date(1990, 4, 3)) == "April third"

    def test_years(self) -> None:
        assert spoken_year(1978) == "nineteen seventy-eight"
        assert spoken_year(2010) == "twenty ten"
