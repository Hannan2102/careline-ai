"""Spelling a name back, and reading one that was spelled.

Found in a demo: a caller who was certainly on file failed verification twice,
because the recogniser had written "Smyth" for "Smith" and nothing in the call
ever told them so. Spelling the name back is how they find out; reading their
spelling is how they fix it.
"""

from __future__ import annotations

import pytest

from app.agents.spelling import (
    NameParts,
    parse_spelled,
    spell_for_speech,
    spell_written,
    split_name,
    tidy_name,
)


class TestWrittenForm:
    @pytest.mark.parametrize(
        ("name", "written"),
        [
            ("John", "J-O-H-N"),
            ("smith", "S-M-I-T-H"),
            ("O'Brien", "O, apostrophe, B, R, I, E, N"),
            ("Smith-Jones", "S, M, I, T, H, hyphen, J, O, N, E, S"),
            ("van der Berg", "V, A, N, space, D, E, R, space, B, E, R, G"),
        ],
    )
    def test_letters_and_the_characters_between_them(self, name: str, written: str) -> None:
        assert spell_written(name) == written


class TestSpokenForm:
    """Commas, and a pause where Aura runs letters together (spelling.py)."""

    def test_letters_are_separated_by_commas(self) -> None:
        assert spell_for_speech("J-O-H-N") == "J, O, H, N"

    def test_a_pause_before_a_vowel_that_follows_a_vowel(self) -> None:
        """Bilzerian, from a live call: the A after the I was swallowed."""
        assert spell_for_speech("B-I-L-Z-E-R-I-A-N") == "B, I, L, Z, E, R, I... A, N"

    def test_a_pause_before_every_a(self) -> None:
        """Even after a consonant, A was spoken as a clipped "uh"."""
        assert spell_for_speech("H-A-N-N-A-N") == "H... A, N, N... A, N"

    def test_named_characters_are_kept(self) -> None:
        assert spell_for_speech("O, apostrophe, B, R, I, E, N") == "O, apostrophe, B, R, I... E, N"


class TestReadingASpelling:
    @pytest.mark.parametrize(
        "heard",
        [
            "S M Y T H",
            "s m y t h",
            "s-m-y-t-h",
            "S. M. Y. T. H.",
            "No, it's S M Y T H",
            "my last name is spelled s m y t h, thanks",
            "es em why tee aitch",
            "S as in Sam, M as in Mary, Y as in yellow, T as in Tom, H as in Harry",
            "S for sugar M for mother Y for yes T for tango H for hotel",
            "sierra mike yankee tango hotel",
            "capital S m y t h",
        ],
    )
    def test_the_ways_a_recogniser_writes_letters(self, heard: str) -> None:
        assert parse_spelled(heard) == "SMYTH"

    @pytest.mark.parametrize(
        ("heard", "letters"),
        [
            ("see are why", "CRY"),
            ("bee e are g", "BERG"),
            ("double you a t t", "WATT"),
            ("w a double t", "WATT"),
            ("j o you are", "JOUR"),
            ("dee ex", "DX"),
            ("o apostrophe b r i e n", "O'BRIEN"),
            ("s m i t h hyphen j o n e s", "SMITH-JONES"),
            ("v a n space d e r space b e r g", "VAN DER BERG"),
            ("x-ray oscar", "XO"),
        ],
    )
    def test_homophones_doubles_and_named_characters(self, heard: str, letters: str) -> None:
        assert parse_spelled(heard) == letters

    @pytest.mark.parametrize(
        "heard",
        [
            "No, it's Jon without the h",
            "That's not right",
            "no",
            "yes that's correct",
            "Smyth",
            "",
        ],
    )
    def test_anything_else_is_not_a_spelling(self, heard: str) -> None:
        """Reading letters out of a sentence would invent a name."""
        assert parse_spelled(heard) is None

    def test_a_single_word_counts_when_a_spelling_was_asked_for(self) -> None:
        """The recogniser sometimes merges the letters back into the word."""
        assert parse_spelled("SMYTH", allow_word=True) == "SMYTH"
        assert parse_spelled("it's Smyth", allow_word=True) == "SMYTH"


class TestNameParts:
    @pytest.mark.parametrize(
        ("said", "parts"),
        [
            ("john smith", NameParts(given="John", family="Smith")),
            ("Mary Ann O'Brien", NameParts(given="Mary", middle="Ann", family="O'Brien")),
            ("maria van der berg", NameParts(given="Maria", family="Van Der Berg")),
            ("John", NameParts(given="John")),
        ],
    )
    def test_given_middle_family(self, said: str, parts: NameParts) -> None:
        assert split_name(said) == parts

    def test_tidy_capitalisation(self) -> None:
        assert tidy_name("o'brien") == "O'Brien"
        assert tidy_name("SMITH-JONES") == "Smith-Jones"
