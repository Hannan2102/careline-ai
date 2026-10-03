"""Every call opens with identity (ADR 010).

The demo that prompted this found two things. A caller who was certainly on
file failed twice, because the recogniser had written a spelling of their name
nobody had said and nothing in the call told them. And a date given as digits
was read day-first without a word, so "03/04" became April at a US clinic.

So: the name is spelled back, letter by letter; the date of birth is read back
with the month as a word; an ambiguous date is asked about, never picked; and
only then is anything checked against the record -- by exactly the same
verification as before, with exactly the same replies to a failure.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from tests.conftest import JOHN_SMITH, verify_by_conversation

from app.agents.factory import Runtime, build_runtime
from app.agents.orchestrator import ASK_NAME_EXISTING, ASK_STATUS_AGAIN
from app.agents.state import SessionChannel, SessionState
from app.config.clinic import CLINIC_NAME, GREETING
from app.config.settings import Settings
from app.ehr.base import EHRProvider
from app.schemas.domain import AuditAction, EscalationCategory
from app.workflows.identity import IDENTITY_HANDOVER, RETRY_IDENTITY

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


@pytest.fixture
def runtime(memory_ehr: EHRProvider) -> Runtime:
    return build_runtime(ehr=memory_ehr, settings=Settings(_env_file=None, app_env="test"))


@pytest.fixture
def session(runtime: Runtime) -> SessionState:
    return runtime.sessions.create(channel=SessionChannel.VOICE)


async def say(runtime: Runtime, session: SessionState, *lines: str) -> str:
    message = ""
    for line in lines:
        message = (await runtime.orchestrator.handle_turn(session, line, now=NOW)).message
    return message


def actions(runtime: Runtime, session: SessionState) -> list[AuditAction]:
    return [e.action for e in runtime.audit.store.all() if e.session_id == session.session_id]


class TestTheOpening:
    def test_the_greeting_asks_whether_they_are_a_patient(self) -> None:
        assert CLINIC_NAME in GREETING
        assert "automated assistant" in GREETING
        assert GREETING.endswith(
            "Are you an existing patient, or are you new and would like to register?"
        )
        assert "How can I help" not in GREETING

    @pytest.mark.parametrize(
        "answer",
        [
            "I'm an existing patient",
            "existing",
            "Yes, I've been there before",
            "I'm a patient there",
        ],
    )
    async def test_an_existing_patient_is_asked_for_their_name(
        self, runtime: Runtime, session: SessionState, answer: str
    ) -> None:
        assert await say(runtime, session, answer) == ASK_NAME_EXISTING

    @pytest.mark.parametrize(
        "answer", ["I'm new", "new", "I'd like to register", "No, it's my first time"]
    )
    async def test_a_new_patient_goes_to_registration(
        self, runtime: Runtime, session: SessionState, answer: str
    ) -> None:
        assert await say(runtime, session, answer) == (
            "Happy to get you registered. Could I take your first and last name?"
        )

    async def test_giving_a_name_straight_away_counts_as_existing(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        assert "J-O-H-N" in await say(runtime, session, "John Smith")

    async def test_a_request_only_a_patient_can_make_skips_the_question(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        assert await say(runtime, session, "I need to cancel my appointment") == (
            "I can help with that. First, could I take your first and last name?"
        )

    async def test_an_unclear_answer_is_asked_again_as_yes_or_no(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        assert await say(runtime, session, "Hello?") == ASK_STATUS_AGAIN
        assert await say(runtime, session, "No") == (
            "Happy to get you registered. Could I take your first and last name?"
        )

    async def test_the_happy_path_word_for_word(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        assert await say(runtime, session, "John Smith") == (
            "Thanks. I have your first name as J-O-H-N and your last name as S-M-I-T-H. "
            "Is that right?"
        )
        assert await say(runtime, session, "Yes") == "Thank you, John. And your date of birth?"
        assert await say(runtime, session, "February fifteenth, nineteen eighty-five") == (
            "I have the fifteenth of February, nineteen eighty-five. Is that correct?"
        )
        assert await say(runtime, session, "That's right") == (
            "Thanks, John, you're verified. How can I help you today?"
        )
        assert session.patient_ref == JOHN_SMITH

    async def test_verification_runs_once_and_only_after_both_are_confirmed(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        await say(runtime, session, "John Smith", "yes", "15 February 1985")
        assert AuditAction.VERIFICATION_ATTEMPTED not in actions(runtime, session)
        await say(runtime, session, "yes")
        assert actions(runtime, session).count(AuditAction.VERIFICATION_ATTEMPTED) == 1

    async def test_a_name_and_date_in_one_breath_are_still_both_read_back(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        spelled = await say(runtime, session, "I'm John Smith, born 15 February 1985")
        assert "J-O-H-N" in spelled
        read_back = await say(runtime, session, "yes")
        assert read_back == (
            "Thank you, John. I have the fifteenth of February, nineteen eighty-five. "
            "Is that correct?"
        )
        assert not session.is_verified
        await say(runtime, session, "yes")
        assert session.is_verified

    async def test_a_middle_name_is_spelled_and_named(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        message = await say(runtime, session, "Mary Ann O'Brien")
        assert message == (
            "Thanks. I have your first name as M-A-R-Y, your middle name as A-N-N and "
            "your last name as O, apostrophe, B, R, I, E, N. Is that right?"
        )


class TestCorrectingTheName:
    async def test_no_then_a_spelling(self, runtime: Runtime, session: SessionState) -> None:
        await say(runtime, session, "John Smyth")
        assert await say(runtime, session, "no") == (
            "Sorry about that. Could you spell your last name for me?"
        )
        assert "S-M-I-T-H" in await say(runtime, session, "S M I T H")
        await say(runtime, session, "yes", "15 February 1985", "yes")
        assert session.is_verified

    async def test_no_and_the_correction_together(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        await say(runtime, session, "John Smyth")
        message = await say(runtime, session, "No, it's S as in Sam, M, I, T, H")
        assert "your last name as S-M-I-T-H" in message
        assert "first name as J-O-H-N" in message

    async def test_a_spelling_in_the_nato_alphabet(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        await say(
            runtime,
            session,
            "Jon Smith",
            "no",
        )
        # "Jon" is closer to the first name, but nothing said which part was
        # wrong, so the last name is asked for first.
        message = await say(runtime, session, "no, my first name is juliet oscar hotel november")
        assert "first name as J-O-H-N" in message

    async def test_a_free_form_correction_asks_for_a_spelling_of_that_part(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        """ "Without the h" is understood by people, not by a parser. Ask."""
        await say(runtime, session, "John Smith")
        assert await say(runtime, session, "No, it's Jon without the h") == (
            "Sorry about that. Could you spell your first name for me?"
        )
        assert "first name as J-O-N" in await say(runtime, session, "j o n")

    async def test_a_bare_no_asks_for_the_last_name_then_the_first(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        await say(runtime, session, "Jon Smyth")
        assert "last name" in await say(runtime, session, "no")
        await say(runtime, session, "s m i t h")
        assert "first name" in await say(runtime, session, "no")

    async def test_three_rejections_hand_over(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        message = await say(
            runtime, session, "Jon Smyth", "no", "s m y t h", "no", "s m i t h", "no"
        )
        assert message == IDENTITY_HANDOVER
        escalation = runtime.escalations.store.for_session(session.session_id)[0]
        assert escalation.category is EscalationCategory.SYSTEM_UNCERTAINTY
        assert "name could not be confirmed" in escalation.summary
        assert actions(runtime, session).count(AuditAction.IDENTITY_NOT_CONFIRMED) == 3
        assert AuditAction.VERIFICATION_ATTEMPTED not in actions(runtime, session)

    async def test_after_the_handover_nothing_behind_the_gate_opens(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        await say(runtime, session, "Jon Smyth", "no", "s m y t h", "no", "s m i t h", "no")
        assert await say(runtime, session, "When is my appointment?") == IDENTITY_HANDOVER
        assert not session.is_verified


class TestTheDateOfBirth:
    async def test_an_ambiguous_date_is_asked_about_and_resolved(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        """Maria Garcia is on file as 3 November 1972; "11/03/1972" is two dates."""
        await say(runtime, session, "Maria Garcia", "yes")
        assert await say(runtime, session, "11/03/1972") == (
            "Just to check — is that March eleventh or November third?"
        )
        assert await say(runtime, session, "November") == (
            "I have the third of November, nineteen seventy-two. Is that correct?"
        )
        await say(runtime, session, "yes")
        assert session.is_verified

    async def test_an_ambiguous_date_by_position(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        await say(runtime, session, "Maria Garcia", "yes", "zero three one one seventy two")
        assert "the eleventh of March" in await say(runtime, session, "the first one")

    async def test_a_rejected_read_back_is_asked_again_and_not_reused(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        await say(runtime, session, "John Smith", "yes", "16 February 1985")
        assert await say(runtime, session, "no") == (
            "Sorry about that. Could I take your date of birth again?"
        )
        message = await say(runtime, session, "15 February 1985")
        assert "fifteenth of February" in message
        assert "sixteenth" not in message
        await say(runtime, session, "yes")
        assert session.is_verified

    async def test_a_date_that_cannot_be_read_gets_a_hint(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        await say(runtime, session, "John Smith", "yes")
        message = await say(runtime, session, "the day after the queen's birthday")
        assert message.startswith("Sorry, I didn't catch that. Could you say it as the month")

    async def test_three_unusable_dates_hand_over(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        message = await say(
            runtime, session, "John Smith", "yes", "dunno", "I can't remember", "no idea"
        )
        assert message == IDENTITY_HANDOVER
        assert "date of birth could not be confirmed" in (
            runtime.escalations.store.for_session(session.session_id)[0].summary
        )
        assert AuditAction.VERIFICATION_ATTEMPTED not in actions(runtime, session)

    async def test_a_future_date_is_not_a_date_of_birth(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        await say(runtime, session, "John Smith", "yes")
        assert "didn't catch that" in await say(runtime, session, "December 1st 2030")


class TestVerification:
    async def test_unknown_and_wrong_details_get_the_same_reply(
        self, runtime: Runtime, memory_ehr: EHRProvider
    ) -> None:
        """ADR 003, unchanged: the difference between the two is a disclosure."""
        unknown = runtime.sessions.create()
        wrong = runtime.sessions.create()
        a = await say(runtime, unknown, "Jane Doe", "yes", "1 January 1970", "yes")
        b = await say(runtime, wrong, "John Smith", "yes", "16 February 1985", "yes")
        assert a == b == RETRY_IDENTITY

    async def test_the_retry_runs_the_same_steps(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        await say(runtime, session, "John Smith", "yes", "16 February 1985", "yes")
        assert "J-O-H-N" in await say(runtime, session, "John Smith")
        assert "date of birth" in await say(runtime, session, "yes")
        await say(runtime, session, "15 February 1985", "yes")
        assert session.is_verified

    async def test_three_failures_still_lock_out(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        wrong = ("Jane Doe", "yes", "1 January 1970", "yes")
        await say(runtime, session, *wrong, *wrong)
        final = await say(runtime, session, *wrong)
        assert "front desk" in final
        assert session.is_locked_out
        escalation = runtime.escalations.store.for_session(session.session_id)[0]
        assert escalation.category is EscalationCategory.FAILED_VERIFICATION

    async def test_a_shared_name_and_date_asks_for_a_second_factor(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        """Two Robert Johnsons share a birthday in the seed data, deliberately."""
        message = await say(runtime, session, "Robert Johnson", "yes", "June 21st 1990", "yes")
        assert "last four digits" in message
        verified = await say(runtime, session, "zero four one one")
        assert verified.startswith("Thanks, Robert, you're verified.")
        assert session.patient_ref is not None

    async def test_a_failed_verification_never_registers_anybody_on_its_own(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        await say(runtime, session, "Nina Okafor", "yes", "3 May 1990", "yes")
        assert AuditAction.PATIENT_REGISTERED not in actions(runtime, session)
        assert not session.is_verified


class TestWhatTheyRangAbout:
    async def test_a_request_made_first_is_acted_on_once_verified(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        assert await say(runtime, session, "I need to move my appointment") == (
            "I can help with that. First, could I take your first and last name?"
        )
        message = await verify_by_conversation(runtime.orchestrator, session, now=NOW)
        assert message.startswith("Thanks, John, you're verified.")
        assert "I can move that to" in message
        assert "How can I help" not in message

    async def test_a_request_and_a_name_in_one_breath(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        spelled = await say(runtime, session, "I need to reschedule, my name is John Smith")
        assert "J-O-H-N" in spelled
        message = await say(runtime, session, "yes", "15 February 1985", "yes")
        assert "I can move that to" in message

    async def test_nothing_asked_for_means_asking(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        message = await verify_by_conversation(runtime.orchestrator, session, now=NOW)
        assert message == "Thanks, John, you're verified. How can I help you today?"
        assert "refill" in (await say(runtime, session, "I need a refill on my metformin"))


class TestNewPatients:
    async def test_after_a_failed_match_the_confirmed_details_are_reused(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        """They spelled it and confirmed it; asking a third time is not listening."""
        await say(runtime, session, "Nina Okafor", "yes", "3 May 1990", "yes")
        message = await say(runtime, session, "I've never been to the clinic before")
        assert "phone number" in message
        assert "name" not in message.lower()

        await say(runtime, session, "five five five oh one nine oh two three four", "A check-up")
        patient = await runtime.ehr.get_patient(str(session.patient_ref))
        assert (patient.given_name, patient.family_name) == ("Nina", "Okafor")
        assert patient.date_of_birth == date(1990, 5, 3)

    async def test_saying_so_mid_collection_continues_where_it_was(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        await say(runtime, session, "Nina Okafor", "yes")
        message = await say(runtime, session, "Actually I'm a new patient")
        assert message == "Thank you, Nina. And your date of birth?"
        assert "May" in await say(runtime, session, "the third of May nineteen ninety")
        assert "phone number" in await say(runtime, session, "yes")

    async def test_registration_from_the_start_uses_the_same_read_backs(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        assert "registered" in await say(runtime, session, "Can I join the practice?")
        assert "N-I-N-A" in await say(runtime, session, "Nina Okafor")
        assert "date of birth" in await say(runtime, session, "yes")
        assert "Is that correct?" in await say(runtime, session, "3 May 1990")
        assert "phone number" in await say(runtime, session, "yes")


class TestTheFaqSwitch:
    async def test_off_by_default(self) -> None:
        assert Settings(_env_file=None).identity_first_allow_faq is False

    async def test_on_a_clinic_question_first_then_a_name_for_anything_else(
        self, memory_ehr: EHRProvider
    ) -> None:
        runtime = build_runtime(
            ehr=memory_ehr,
            settings=Settings(_env_file=None, app_env="test", identity_first_allow_faq=True),
        )
        session = runtime.sessions.create()
        answer = await say(runtime, session, "Is there parking?")
        assert "parking" in answer.lower()
        assert not session.is_verified

        assert await say(runtime, session, "yes") == (
            "Of course. First, could I take your first and last name?"
        )
        assert "J-O-H-N" in await say(runtime, session, "John Smith")

    async def test_on_but_no_thanks_still_ends_the_call(self, memory_ehr: EHRProvider) -> None:
        runtime = build_runtime(
            ehr=memory_ehr,
            settings=Settings(_env_file=None, app_env="test", identity_first_allow_faq=True),
        )
        session = runtime.sessions.create()
        await say(runtime, session, "What are your hours?")
        assert "Take care" in await say(runtime, session, "no thanks")
        assert not session.is_active


class TestASpellingSplitAcrossTurns:
    """The first live call, replayed in text with a synthetic name.

    Whatever the voice layer does about pauses, the recogniser can still split
    a spelling in two. The second half has to be joined on, and a single letter
    must never become a name.
    """

    async def test_the_tail_of_a_spelling_is_joined_on(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        await say(runtime, session, "Nina Okafr", "No.")
        assert "O-K-A" in await say(runtime, session, "O k a")
        assert "your last name as O-K-A-F-O-R" in await say(runtime, session, "f o r")
        assert not [
            a for a in actions(runtime, session)[1:] if a is AuditAction.IDENTITY_NOT_CONFIRMED
        ], "joining the rest of a spelling counted as a rejection"
        await say(runtime, session, "yes")
        assert session.workflow_state.get("_identity.confirmed_name") == {
            "given": "Nina",
            "middle": None,
            "family": "Okafor",
        }

    async def test_a_single_letter_is_never_a_name(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        await say(runtime, session, "Nina Okafor")
        message = await say(runtime, session, "n")
        assert "your last name as N." not in message
        assert "spell" in message

    async def test_a_merged_first_and_last_name_can_be_put_right(
        self, runtime: Runtime, session: SessionState
    ) -> None:
        """ "Ninaokafor" heard as one first name, then "Okafor" as the last."""
        await say(runtime, session, "existing")
        assert (
            await say(runtime, session, "Ninaokafor") == "Thanks, Ninaokafor. And your last name?"
        )
        await say(runtime, session, "Okafor", "no", "o k a f o r", "no")
        message = await say(runtime, session, "n i n a")
        assert "first name as N-I-N-A and your last name as O-K-A-F-O-R" in message
