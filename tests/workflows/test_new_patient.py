"""Registering somebody the clinic has never seen.

The only workflow that writes a patient record, and the only one whose
precondition is *not* being in the record — so the safety argument runs the
other way round and has to be tested that way round.

What makes it defensible is that a record created during the call contains
nothing but what the caller has just said. There is no history behind the gate
to protect, which is why registration may open it. Everything below is a way
of checking that this stays true: no record is written for someone who already
has one, a failed verification never becomes a registration, and nothing is
asked for that a receptionist would not ask over the phone.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from tests.conftest import JOHN_SMITH_DOB

from app.agents.factory import Runtime, build_runtime
from app.agents.intents import Intent
from app.agents.state import SessionState
from app.config.settings import Settings
from app.ehr.base import EHRProvider
from app.schemas.domain import AppointmentType, AuditAction
from app.workflows.base import WorkflowStatus

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


@pytest.fixture
def runtime(memory_ehr: EHRProvider) -> Runtime:
    return build_runtime(ehr=memory_ehr, settings=Settings(_env_file=None, app_env="test"))


@pytest.fixture
def session() -> SessionState:
    return SessionState(session_id="sess-new", created_at=NOW)


async def say(runtime: Runtime, session: SessionState, *lines: str) -> str:
    message = ""
    for line in lines:
        message = (await runtime.orchestrator.handle_turn(session, line, now=NOW)).message
    return message


REGISTER = (
    "I've never been to this clinic before, can I get an appointment?",
    "Nina Okafor",
    "Third of May nineteen ninety",
    "Five five five, oh one nine, oh two three four",
    "A check-up",
)


class TestAStrangerCanBecomeAPatient:
    async def test_the_whole_thing_end_to_end(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        offered = await say(runtime, session, *REGISTER)
        assert "you're registered" in offered.lower()

        booked = await say(runtime, session, "The first one, please", "Yes")

        assert "You're booked in" in booked
        assert session.is_verified
        patient = await runtime.ehr.get_patient(str(session.patient_ref))
        assert patient.family_name == "Okafor"

    async def test_the_first_visit_is_the_long_one(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        """45 minutes, and the type cannot be inferred from any reason given.

        A first appointment is longer because it is a first appointment, so
        the booking workflow is told the type rather than left to classify
        "a check-up" into a fifteen-minute slot.
        """
        await say(runtime, session, *REGISTER, "The first one, please", "Yes")

        appointments = await runtime.ehr.list_appointments(NOW.date(), NOW.date() + _WEEK)
        mine = [a for a in appointments if a.patient_ref == session.patient_ref]
        assert [a.appointment_type for a in mine] == [AppointmentType.NEW_PATIENT]

    async def test_they_are_told_to_bring_identification(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        """The record was made from what they said. The desk checks it.

        Registration over the phone is not identity proofing and this project
        does not pretend otherwise; this sentence is where the loop closes.
        """
        booked = await say(runtime, session, *REGISTER, "The first one, please", "Yes")

        assert "photo ID" in booked
        assert "20 minutes early" in booked

    async def test_the_reason_is_asked_for_and_kept(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        """Otherwise the visit note is whatever sentence opened the call."""
        await say(runtime, session, *REGISTER, "The first one, please", "Yes")

        appointments = await runtime.ehr.list_appointments(NOW.date(), NOW.date() + _WEEK)
        mine = next(a for a in appointments if a.patient_ref == session.patient_ref)
        assert mine.reason == "A check-up"

    async def test_details_volunteered_all_at_once_are_kept(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        """People do not answer one question at a time."""
        asked = await say(
            runtime,
            session,
            "I'm new here — I'm Nina Okafor, born the third of May nineteen ninety",
        )
        assert "phone number" in asked, f"asked for something already given: {asked}"


class TestNoSecondRecordForSomebodyWhoHasOne:
    async def test_matching_details_stop_before_the_write(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        """A duplicate is the failure that matters here.

        The history sits under the old reference, so a clinician reading the
        new one sees a patient with no allergies and no medications. Merging
        records is not something to attempt over the phone.
        """
        before = len(await runtime.ehr.list_patients())

        answer = await say(
            runtime,
            session,
            "I'd like to register with the practice",
            "John Smith",
            f"{JOHN_SMITH_DOB:%d %B %Y}",
        )

        assert "front desk" in answer
        assert len(await runtime.ehr.list_patients()) == before, "created a second record"
        assert not session.is_verified, "a duplicate check is not a verification"

    async def test_it_does_not_hand_over_the_existing_record(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        """Being told 'we have you' is not being let in.

        The caller has proved nothing — they have supplied a name and a date
        of birth, which is what everyone who fails verification supplies.
        """
        await say(
            runtime,
            session,
            "I'd like to register",
            "John Smith",
            f"{JOHN_SMITH_DOB:%d %B %Y}",
        )
        answer = await say(runtime, session, "What medication am I on?")

        assert "Metformin" not in answer
        assert session.patient_ref is None


class TestFailedVerificationIsNotARegistration:
    async def test_a_wrong_date_of_birth_does_not_register_anybody(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        """Somebody who misremembers a date is a patient, not a new patient.

        Routing a failed verification into registration would manufacture
        duplicates out of ordinary human error, which is the hazard the
        duplicate check exists to prevent — arriving by the front door.
        """
        before = len(await runtime.ehr.list_patients())

        answer = await say(
            runtime,
            session,
            "I need to check my appointment",
            "John Smith",
            "First of January nineteen ninety nine",
        )

        assert "couldn't find a match" in answer
        assert len(await runtime.ehr.list_patients()) == before

    async def test_but_it_says_registration_exists(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        """Said to everyone who fails, so it signals nothing about the record.

        Without it a genuine new patient asking to book is asked to prove who
        they are, fails, and is handed to the front desk never knowing the
        agent could have registered them.
        """
        answer = await say(
            runtime,
            session,
            "I need to check my appointment",
            "Someone Unknown",
            "First of January nineteen ninety nine",
        )

        assert "not been to the clinic before" in answer

    async def test_and_the_caller_can_take_it(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        await say(
            runtime,
            session,
            "I need to check my appointment",
            "Nina Okafor",
            "Third of May nineteen ninety",
        )

        answer = await say(runtime, session, "I've never been to the clinic before")

        assert "register" in answer.lower()


class TestWhatIsAsked:
    async def test_it_asks_for_four_things_and_no_more(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        """A name, a date of birth, a phone number, and what it is about.

        No insurance identifier, no social security number, nothing a
        receptionist would only take with a photo ID in front of them. The
        list is short on purpose and this test is what keeps it short.
        """
        asked: list[str] = []
        for line in REGISTER:
            asked.append((await runtime.orchestrator.handle_turn(session, line, now=NOW)).message)

        wanted = " ".join(asked).lower()
        for forbidden in ("insurance", "social security", "policy number", "member number"):
            assert forbidden not in wanted, f"asked a new caller for their {forbidden}"

    async def test_the_write_is_audited(self, runtime: Runtime, session: SessionState) -> None:
        """A patient created over the phone is exactly what gets queried later."""
        await say(runtime, session, *REGISTER)

        actions = [e.action for e in runtime.audit.store.all()]
        assert AuditAction.PATIENT_SEARCHED in actions
        assert AuditAction.PATIENT_REGISTERED in actions


class TestTellingTheTwoNewPatientQuestionsApart:
    """ "I'm a new patient" is a fact; "I'm a new patient, what happens?" is a
    question about the process. They share their only distinctive words."""

    @pytest.mark.parametrize(
        "utterance",
        [
            "I'm a new patient, what happens?",
            "I'm a new patient what happens",
            "As a new patient what do I need to do?",
        ],
    )
    async def test_the_question_is_answered_not_acted_on(
        self, runtime: Runtime, session: SessionState, utterance: str
    ) -> None:
        answer = await say(runtime, session, utterance)
        assert "45-minute" in answer
        assert "full name" not in answer

    @pytest.mark.parametrize(
        "utterance",
        [
            "I'm a new patient and I'd like to book",
            "I've never been before, can I get an appointment?",
            "Can I join the practice?",
            "I'd like to get on your books",
        ],
    )
    async def test_the_request_starts_a_registration(
        self, runtime: Runtime, session: SessionState, utterance: str
    ) -> None:
        answer = await say(runtime, session, utterance)
        assert "full name" in answer


class TestWhatTheCallerSaysCanBeWrong:
    async def test_a_date_of_birth_in_the_future_is_not_written(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        before = len(await runtime.ehr.list_patients())

        answer = await say(
            runtime, session, "I'm a new patient", "Sam Doyle", "Third of May twenty forty"
        )

        assert "date of birth" in answer
        assert len(await runtime.ehr.list_patients()) == before

    async def test_a_phone_number_too_short_is_asked_for_again(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        answer = await say(
            runtime,
            session,
            "I'm a new patient",
            "Sam Doyle",
            "Third of May nineteen ninety",
            "one two three",
        )

        assert "phone number" in answer

    async def test_a_clinical_question_mid_registration_is_still_refused(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        """Safety runs before extraction, and registration does not change that."""
        result = await runtime.orchestrator.handle_turn(
            session,
            "I'm a new patient — should I double my blood pressure tablets?",
            now=NOW,
        )

        assert result.trace.intent is not Intent.NEW_PATIENT
        assert result.trace.workflow_status != WorkflowStatus.AWAITING_INPUT.value


_WEEK = date(2026, 9, 15) - date(2026, 9, 8)
