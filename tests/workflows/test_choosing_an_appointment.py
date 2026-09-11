"""Picking which appointment, when there is more than one.

Every case here came off one live call, and the middle one is why the others
are written down: the caller said "Tuesday" to choose between two
appointments and was offered the *Wednesday* one to cancel. Two separate
things had to go wrong. Slot times offered during a booking eight turns
earlier were still in memory and resolved a choice they had nothing to do
with; and with those gone, the trailing "one" in "the Tuesday one" was read as
the number one.

Wrong while sounding certain is the worst answer available here, because the
next thing the agent does is cancel something.
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta

import pytest
from tests.conftest import JOHN_SMITH, JOHN_SMITH_DOB, SEED_NOW

from app.agents.factory import build_runtime
from app.agents.orchestrator import Orchestrator
from app.agents.state import SessionState
from app.config.settings import Settings
from app.ehr.base import EHRProvider
from app.schemas.domain import AppointmentType
from app.services.scheduling_service import SchedulingService
from app.utils.formatting import WEEKDAYS, local


def _days_in(listing: str) -> Counter[str]:
    """The weekdays the agent actually read out, and how often it said each."""
    return Counter(
        {
            day.capitalize(): listing.count(day.capitalize())
            for day in WEEKDAYS
            if day.capitalize() in listing
        }
    )


async def _say(orchestrator: Orchestrator, session: SessionState, *lines: str) -> str:
    message = ""
    for line in lines:
        message = (await orchestrator.handle_turn(session, line, now=SEED_NOW)).message
    return message


def _first_option_not_on(offered: str, day: str) -> int:
    """The number of the first offered time that falls on a different day."""
    for position, option in enumerate(offered.split(")")[1:], start=1):
        if day not in option:
            return position
    raise AssertionError(f"every time offered was on a {day}: {offered!r}")


def _named_once(listing: str) -> str:
    """A day that identifies exactly one of them, so the choice is unambiguous."""
    for day, count in _days_in(listing).items():
        if count == 1:
            return day
    raise AssertionError(f"no day distinguishes the options in {listing!r}")


def _named_never(listing: str) -> str:
    """A day none of them falls on."""
    present = _days_in(listing)
    for day in WEEKDAYS:
        if day.capitalize() not in present:
            return day.capitalize()
    raise AssertionError("every day of the week was offered")


@pytest.fixture
def orchestrator(memory_ehr: EHRProvider) -> Orchestrator:
    return build_runtime(
        ehr=memory_ehr, settings=Settings(_env_file=None, app_env="test")
    ).orchestrator


class TestChoosingByTheDayItNamed:
    """The listing names days, so callers answer with a day.

    A day the caller named has to decide the answer by itself -- including
    deciding that there isn't one.
    """

    @pytest.fixture
    async def listed(
        self, orchestrator: Orchestrator, memory_ehr: EHRProvider
    ) -> tuple[SessionState, str]:
        """Two appointments on different days, and the listing that offers them.

        Different days on purpose, and booked directly rather than through the
        conversation: a day can only identify an appointment when the other is
        not also on it, and the times the booking flow offers first all fall on
        the same morning -- so a fixture built by talking would pass this
        file's central test for the wrong reason.
        """
        scheduling = SchedulingService(memory_ehr)
        existing = (await scheduling.get_upcoming_appointments(JOHN_SMITH, now=SEED_NOW))[0]
        offers = await scheduling.find_offers(
            AppointmentType.FOLLOW_UP,
            start_date=SEED_NOW.date(),
            end_date=SEED_NOW.date() + timedelta(days=14),
            count=40,
            now=SEED_NOW,
        )
        elsewhere = next(
            slot for slot in offers if local(slot.start).date() != local(existing.start).date()
        )
        await scheduling.book(JOHN_SMITH, elsewhere.slot_id, AppointmentType.FOLLOW_UP)

        session = SessionState(session_id="sess-choose", created_at=SEED_NOW)
        listing = await _say(
            orchestrator,
            session,
            "What appointments do I have?",
            f"John Smith, born {JOHN_SMITH_DOB:%d %B %Y}",
        )
        assert "Which one did you mean?" in listing
        return session, listing

    async def test_a_day_they_have_picks_that_one(
        self, orchestrator: Orchestrator, listed: tuple[SessionState, str]
    ) -> None:
        session, listing = listed
        day = _named_once(listing)

        result = await orchestrator.handle_turn(session, f"Cancel the {day} one", now=SEED_NOW)

        assert day in result.message
        assert "Shall I cancel it?" in result.message

    async def test_a_day_they_do_not_have_asks_again(
        self, orchestrator: Orchestrator, listed: tuple[SessionState, str]
    ) -> None:
        """Never the first on the list, which is what "one" used to mean here.

        Offering to cancel an appointment on a different day from the one the
        caller named is the worst available answer: it is wrong, and it is
        wrong while sounding certain.
        """
        session, listing = listed
        absent = _named_never(listing)

        result = await orchestrator.handle_turn(session, f"Cancel the {absent} one", now=SEED_NOW)

        assert "which one did you mean" in result.message.lower()
        assert "Shall I cancel" not in result.message

    async def test_an_ordinal_still_works(
        self, orchestrator: Orchestrator, listed: tuple[SessionState, str]
    ) -> None:
        session, listing = listed
        second = listing.split("2)")[1]

        result = await orchestrator.handle_turn(session, "The second one", now=SEED_NOW)

        assert _named_once(second) in result.message or second.split()[0] in result.message


class TestStaleOffersDoNotResolveAChoice:
    async def test_times_offered_in_an_earlier_request_are_not_reused(
        self, orchestrator: Orchestrator
    ) -> None:
        """The booking's slot list must not answer an appointment question.

        It stays in that workflow's memory after the booking completes, and
        resolving against it is resolving against a list the caller was read
        several turns and one workflow ago.
        """
        session = SessionState(session_id="sess-stale", created_at=SEED_NOW)
        for line in (
            "I'd like to book a follow up",
            f"John Smith, born {JOHN_SMITH_DOB:%d %B %Y}",
            "The second one",
            "Yes",
        ):
            await orchestrator.handle_turn(session, line, now=SEED_NOW)
        assert session.workflow_state.get("existing_patient_booking.offers"), (
            "the slot list this test is about was never stored"
        )

        # A new request, whose own workflow has offered nothing.
        await orchestrator.handle_turn(session, "What appointments do I have?", now=SEED_NOW)
        assert session.active_workflow == "appointment_management"
        assert not session.workflow_state.get("appointment_management.offers")

        result = await orchestrator.handle_turn(session, "Tuesday", now=SEED_NOW)
        assert "Shall I" not in result.message, "a stale list resolved a choice"
