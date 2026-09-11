"""Identity given one fact at a time.

Identity used to require the full name and the date of birth in a *single*
utterance; anything given alone was silently discarded, so a caller who
answered "John Smith" and then gave their date of birth was asked for both
again, indefinitely.

Every existing test passed throughout, because every one of them said
"My name is John Smith and I was born 15 February 1985" in one breath. Nobody
speaks that way, least of all out loud, and the bug was invisible until a real
voice call ran into it.
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


async def converse(runtime, utterances: list[str]) -> SessionState:
    session = runtime.sessions.create(channel=SessionChannel.VOICE)
    for utterance in utterances:
        await runtime.orchestrator.handle_turn(session, utterance)
    return session


class TestIdentityAcrossTurns:
    async def test_name_then_date_of_birth_verifies(self, runtime) -> None:
        session = await converse(
            runtime,
            [
                "I'd like to book an appointment",
                "My name is John Smith",
                f"I was born {JOHN_SMITH_DOB:%d %B %Y}",
            ],
        )
        assert session.snapshot().is_verified

    async def test_date_of_birth_then_name_verifies(self, runtime) -> None:
        """Order is the caller's choice, not ours."""
        session = await converse(
            runtime,
            ["I need an appointment", f"{JOHN_SMITH_DOB:%d %B %Y}", "John Smith"],
        )
        assert session.snapshot().is_verified

    async def test_a_spoken_date_completes_a_split_identity(self, runtime) -> None:
        """The combination that a real voice call actually produces."""
        session = await converse(
            runtime,
            ["I'd like to book an appointment", "My name is John Smith", SPOKEN_DOB],
        )
        assert session.snapshot().is_verified

    async def test_both_in_one_utterance_still_verifies(self, runtime) -> None:
        """The path every other test uses must not regress."""
        session = await converse(
            runtime,
            [
                "I'd like to book an appointment",
                f"My name is John Smith and I was born {JOHN_SMITH_DOB:%d %B %Y}",
            ],
        )
        assert session.snapshot().is_verified

    async def test_the_agent_asks_only_for_what_is_missing(self, runtime) -> None:
        """Re-asking for both is what made it sound like it was not listening."""
        session = runtime.sessions.create(channel=SessionChannel.VOICE)
        await runtime.orchestrator.handle_turn(session, "I'd like to book an appointment")
        result = await runtime.orchestrator.handle_turn(session, "My name is John Smith")

        assert "date of birth" in result.message.lower()
        assert "full name" not in result.message.lower()

    async def test_a_half_identity_alone_never_verifies(self, runtime) -> None:
        """Remembering half an identity must not lower the bar for the whole."""
        session = await converse(
            runtime, ["I'd like to book an appointment", "My name is John Smith"]
        )
        assert not session.snapshot().is_verified

    async def test_a_corrected_name_replaces_the_remembered_one(self, runtime) -> None:
        """A caller correcting themselves must not be verified as the first name.

        The partials are cleared once a complete attempt is made, so a failed
        attempt cannot leave a stale half behind to be silently reused.
        """
        session = await converse(
            runtime,
            [
                "I'd like to book an appointment",
                "My name is Wrong Person",
                f"I was born {JOHN_SMITH_DOB:%d %B %Y}",
                "My name is John Smith",
                f"I was born {JOHN_SMITH_DOB:%d %B %Y}",
            ],
        )
        assert session.snapshot().is_verified
