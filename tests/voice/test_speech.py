"""Written for a screen, spoken for an ear.

"Dr. Sarah Patel" is right in the dashboard and was read aloud as "drive Sarah
Patel" on every appointment offer -- 120 of them across the recorded calls,
because every offered slot names a clinician.
"""

from __future__ import annotations

import pytest

from app.voice.speech import for_speech


class TestTitles:
    @pytest.mark.parametrize(
        ("written", "spoken"),
        [
            ("Dr. Sarah Patel", "Doctor Sarah Patel"),
            ("with Dr. Michael Johnson;", "with Doctor Michael Johnson;"),
            ("Drs. Patel and Chen", "Doctors Patel and Chen"),
            ("Mr. Smith", "Mister Smith"),
            ("Mrs. Garcia", "Missus Garcia"),
        ],
    )
    def test_titles_are_said_in_full(self, written: str, spoken: str) -> None:
        assert for_speech(written) == spoken

    def test_every_occurrence_is_rewritten(self) -> None:
        """A slot offer names a clinician per option."""
        offer = (
            "1) Monday at 8:00 AM with Dr. Emily Chen; "
            "2) Tuesday at 8:00 AM with Dr. Michael Johnson"
        )
        assert "Dr." not in for_speech(offer)
        assert for_speech(offer).count("Doctor") == 2


class TestTheAddress:
    def test_a_state_code_before_a_zip_is_spelled_out(self) -> None:
        written = "Oakwood Family Medicine, 1420 Oakwood Avenue, Riverton, OH 45042"
        assert "Ohio 45042" in for_speech(written)

    def test_the_same_letters_elsewhere_are_left_alone(self) -> None:
        """Only a state code sits immediately before a five-digit ZIP."""
        assert for_speech("OH is not a state here") == "OH is not a state here"


class TestRestraint:
    """What this deliberately does not do.

    A general abbreviation expander has to decide whether "St." is Street or
    Saint, and saying the wrong one aloud is worse than the abbreviation it
    replaced. Only forms this clinic's data actually produces are rewritten.
    """

    @pytest.mark.parametrize(
        "written",
        [
            "Please arrive about 15 minutes early.",
            "We're open Monday through Friday, 8:00 AM to 5:00 PM.",
            "Metformin 500 mg, One tablet twice daily with meals.",
            "I can send a refill request to the clinic.",
            "",
        ],
    )
    def test_ordinary_text_is_untouched(self, written: str) -> None:
        assert for_speech(written) == written

    def test_a_name_that_merely_looks_like_a_title_survives(self) -> None:
        """Matching is whole-word, so a real surname is not mangled."""
        assert for_speech("Andrew Drive") == "Andrew Drive"
        assert for_speech("Drury Lane") == "Drury Lane"
