"""Identity given one fact at a time.

Identity used to require the full name and the date of birth in a *single*
utterance; anything given alone was silently discarded, so a caller who
answered "John Smith" and then gave their date of birth was asked for both
again, indefinitely.

Every existing test passed throughout, because every one of them said
"My name is John Smith and I was born 15 February 1985" in one breath. Nobody
speaks that way, least of all out loud, and the bug was invisible until a real
voice call ran into it.

Identity is now asked for one step at a time (ADR 010), which makes the split
the normal case. What these tests protect is the other direction: whatever the
caller volunteers early is kept, and never asked for again.
"""

from __future__ import annotations

import pytest
from tests.conftest import JOHN_SMITH_DOB

from app.agents.factory import build_runtime
from app.agents.state import SessionChannel, SessionState
from app.ehr.factory import get_default_memory_store
from app.ehr.seeding import seed_memory_store

SPOKEN_DOB = "the fifteenth of February nineteen eighty five"


@pytest.fixture
async def runtime():
    await seed_memory_store(get_default_memory_store())
    return build_runtime()


async def converse(runtime, utterances: list[str]) -> tuple[SessionState, str]:
    session = runtime.sessions.create(channel=SessionChannel.VOICE)
    message = ""
    for utterance in utterances:
        message = (await runtime.orchestrator.handle_turn(session, utterance)).message
    return session, message


class TestIdentityAcrossTurns:
    async def test_name_then_date_of_birth_verifies(self, runtime) -> None:
        session, _ = await converse(
            runtime,
            [
                "I'd like to book an appointment",
                "My name is John Smith",
                "yes",
                f"I was born {JOHN_SMITH_DOB:%d %B %Y}",
                "yes",
            ],
        )
        assert session.snapshot().is_verified

    async def test_date_of_birth_then_name_verifies(self, runtime) -> None:
        """Order is the caller's choice, not ours: the early date is kept."""
        session, message = await converse(
            runtime,
            [
                "I need an appointment",
                "I'm an existing patient",
                f"{JOHN_SMITH_DOB:%d %B %Y}",
                "John Smith",
                "yes",
            ],
        )
        # Straight to the read-back -- the date was already given.
        assert "fifteenth of February, nineteen eighty-five" in message
        await runtime.orchestrator.handle_turn(session, "yes")
        assert session.snapshot().is_verified

    async def test_a_spoken_date_completes_a_split_identity(self, runtime) -> None:
        """The combination that a real voice call actually produces."""
        session, _ = await converse(
            runtime,
            ["I'd like to book an appointment", "My name is John Smith", "yes", SPOKEN_DOB, "yes"],
        )
        assert session.snapshot().is_verified

    async def test_both_in_one_utterance_still_verifies(self, runtime) -> None:
        """One breath still works, and is still read back -- twice."""
        session, _ = await converse(
            runtime,
            [
                "I'd like to book an appointment",
                f"My name is John Smith and I was born {JOHN_SMITH_DOB:%d %B %Y}",
                "yes",
                "yes",
            ],
        )
        assert session.snapshot().is_verified

    async def test_the_agent_asks_only_for_what_is_missing(self, runtime) -> None:
        """Re-asking for both is what made it sound like it was not listening."""
        session, _ = await converse(runtime, ["I'd like to book an appointment", "John Smith"])
        result = await runtime.orchestrator.handle_turn(session, "yes")

        assert "date of birth" in result.message.lower()
        assert "name" not in result.message.lower()

    async def test_a_first_name_alone_is_kept(self, runtime) -> None:
        _, message = await converse(runtime, ["existing", "John"])
        assert message == "Thanks, John. And your last name?"

        _, message = await converse(runtime, ["existing", "John", "Smith"])
        assert "J-O-H-N" in message
        assert "S-M-I-T-H" in message

    async def test_a_half_identity_alone_never_verifies(self, runtime) -> None:
        """Remembering half an identity must not lower the bar for the whole."""
        session, _ = await converse(
            runtime, ["I'd like to book an appointment", "My name is John Smith", "yes"]
        )
        assert not session.snapshot().is_verified

    async def test_a_corrected_name_replaces_the_remembered_one(self, runtime) -> None:
        """A caller correcting themselves must not be verified as the first name."""
        session, message = await converse(
            runtime,
            [
                "I'd like to book an appointment",
                "My name is Wrong Person",
                "No, my name is John Smith",
            ],
        )
        assert "S-M-I-T-H" in message
        assert "P-E-R-S-O-N" not in message
        for line in ("yes", SPOKEN_DOB, "yes"):
            await runtime.orchestrator.handle_turn(session, line)
        assert session.snapshot().is_verified

    async def test_a_lower_case_transcript_is_a_name_when_one_was_asked_for(self, runtime) -> None:
        """Recognisers do not always capitalise; the caller still answered."""
        _, message = await converse(runtime, ["I'm already a patient", "john smith"])
        assert "J-O-H-N" in message
