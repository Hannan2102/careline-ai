"""Shared identity collection.

Every workflow that touches patient data opens the same way: name and date of
birth, a second factor if the details are ambiguous, and a handover after
repeated failure. Keeping one implementation matters more here than elsewhere
-- divergent copies of a security-relevant path drift, and the wording itself
carries a guarantee (an unknown name and a wrong date of birth must produce
identical replies, in every workflow).

**The steps (ADR 010).** A name is taken, spelled back letter by letter, and
confirmed; then a date of birth is taken, read back with the month as a word,
and confirmed; only then is anything checked against the record. Each step is
a named state, so extraction is told exactly which question is outstanding:

    ASKING_NAME -> CONFIRMING_NAME <-> SPELLING_NAME
                -> ASKING_DOB -> [DISAMBIGUATING_DOB] -> CONFIRMING_DOB
                -> verify -> [AWAITING_SECOND_FACTOR]

Why both read-backs: a demo caller who was certainly on file failed twice,
because the recogniser had written a spelling of their name nobody had said
and nothing in the call told them. A name heard only as sound cannot be
checked by the person who owns it; spelled, it can. And "03/04/1990" is two
different birthdays depending on which side of the Atlantic it was written --
so an ambiguous date is asked about, never picked.

Unconfirmed values and confirmed ones are kept apart (``_identity.name`` and
``_identity.confirmed_name``), so nothing the caller has not agreed to is ever
submitted for verification or written into a new record.

``collect`` runs the steps and stops when both halves are confirmed -- the
registration workflow uses that, since a new patient has nothing to verify
against. ``advance`` is ``collect`` followed by verification, and is what the
orchestrator runs at the start of every call. ``submit_identity`` is the
verification itself, unchanged in what it decides.
"""

from __future__ import annotations

import difflib
import re
from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.agents.dob_parser import MONTH_NAMES, speak_date, speak_month_day
from app.agents.spelling import NameParts, name_from_letters, spell_written, split_name
from app.agents.state import SessionState
from app.observability.logging import get_logger
from app.schemas.domain import AuditAction, EscalationCategory
from app.services.audit_service import AuditService
from app.services.verification_service import (
    SecondFactorType,
    VerificationOutcome,
    VerificationService,
)
from app.workflows.base import AwaitedInput

logger = get_logger(__name__)

# --------------------------------------------------------------------------
# What the caller hears
# --------------------------------------------------------------------------

ASK_NAME = "Could I take your first and last name?"

#: When the caller has said what they want before saying who they are. The
#: request is remembered and acted on once they are verified; saying so is
#: what stops "I need to reschedule" being met with what sounds like a refusal.
ASK_NAME_FOR_REQUEST = "I can help with that. First, could I take your first and last name?"

NAME_NOT_CAUGHT = "Sorry, I didn't catch your name. Could I take your first and last name?"

#: Only a first name arrived. People often give one and wait.
ASK_FAMILY_NAME = "Thanks, {given}. And your last name?"

ASK_SPELLING = "Sorry about that. Could you spell your {part} for me?"
SPELLING_NOT_CAUGHT = (
    "Sorry, I didn't catch that. Could you spell your {part} one letter at a time?"
)

ASK_DATE_OF_BIRTH_AFTER_NAME = "Thank you, {given}. And your date of birth?"

#: Asked when the date could not be read at all, with the shape that parses
#: most reliably. The example is a fixed, obviously illustrative date, so
#: nobody hears it as a guess at theirs.
DOB_NOT_CAUGHT = (
    "Sorry, I didn't catch that. Could you say it as the month, the day, then the "
    "year — for example, January fifteenth, nineteen eighty-five?"
)
ASK_DOB_AGAIN = "Sorry about that. Could I take your date of birth again?"
CONFIRM_DOB = "I have {date}. Is that correct?"
DISAMBIGUATE_DOB = "Just to check — is that {first} or {second}?"

#: Said once the record matched. The rest of the turn is whatever the caller
#: rang about, or a question about it.
VERIFIED = "Thanks, {given}, you're verified."

#: Typed-input prompts, for a workflow handed half an identity.
ASK_DATE_OF_BIRTH = "Thanks. And your date of birth?"
ASK_FULL_NAME = "Thanks. And your first and last name?"

#: Where a half-given identity waits for its other half, on the typed path.
#: Namespaced under the session's workflow state so it travels with the
#: conversation and is cleared with it.
_PARTIAL_NAME = "_identity.full_name"
_PARTIAL_DOB = "_identity.date_of_birth"

#: Deliberately identical whether the record is unknown or the details are
#: wrong. The difference between those two is itself information (ADR 003).
#:
#: It mentions registration, and does not offer it. A caller who has
#: misremembered their date of birth is not a new patient, and an agent that
#: routed a failed verification into registration would manufacture duplicate
#: records out of ordinary human error -- a second record is where a
#: clinician reads no allergies and no medications for a person who has both.
#: So the caller has to say it, and the same sentence is said to everyone who
#: fails, which is what keeps it from being a signal about the record.
#:
#: It asks for the name alone, because the retry runs the same steps as the
#: first attempt: spelled back, then the date of birth read back. Asking for
#: both at once and then spelling only one of them back would be two flows.
RETRY_IDENTITY = (
    "I couldn't find a match for those details. Let's try once more — could I take "
    "your first and last name? Or if you've not been to the clinic before, say so "
    "and I can register you."
)

ASK_SECOND_FACTOR = (
    "Thanks. Could I also take the last four digits of the phone number we have on file?"
)

RETRY_SECOND_FACTOR = (
    "That didn't match what we have on file. Could you try those last four digits again?"
)

LOCKED_OUT = (
    "I haven't been able to confirm your identity, so I'm passing you to our front desk, "
    "who can help verify who you are."
)

#: Three read-backs the caller said were wrong, or three answers that could not
#: be understood. Same tone as LOCKED_OUT, and honest about whose problem it
#: is: the caller knows their own name.
IDENTITY_HANDOVER = (
    "I'm sorry, I haven't been able to take your details down correctly, so I'm passing "
    "you to our front desk, who can help."
)

#: Failed read-backs or unusable answers allowed, per half, before a person
#: takes over. Three, like the verification lockout: enough for one mis-hearing
#: and one bad correction, not enough to become a loop.
MAX_STRIKES = 3

_PART_LABELS = {"given": "first name", "middle": "middle name", "family": "last name"}


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------


class IdentityStep(StrEnum):
    #: "Are you an existing patient, or new?" Asked by the orchestrator, which
    #: owns the choice between verifying and registering; named here so the
    #: trace shows it like every other step.
    ASKING_STATUS = "ASKING_STATUS"
    ASKING_NAME = "ASKING_NAME"
    CONFIRMING_NAME = "CONFIRMING_NAME"
    SPELLING_NAME = "SPELLING_NAME"
    ASKING_DOB = "ASKING_DOB"
    DISAMBIGUATING_DOB = "DISAMBIGUATING_DOB"
    CONFIRMING_DOB = "CONFIRMING_DOB"
    AWAITING_SECOND_FACTOR = "AWAITING_SECOND_FACTOR"
    VERIFIED = "VERIFIED"
    HANDED_OVER = "HANDED_OVER"
    LOCKED_OUT = "LOCKED_OUT"


_AWAITING: dict[IdentityStep, AwaitedInput] = {
    IdentityStep.ASKING_STATUS: AwaitedInput.PATIENT_STATUS,
    IdentityStep.ASKING_NAME: AwaitedInput.NAME,
    IdentityStep.CONFIRMING_NAME: AwaitedInput.NAME_CONFIRMATION,
    IdentityStep.SPELLING_NAME: AwaitedInput.NAME_SPELLING,
    IdentityStep.ASKING_DOB: AwaitedInput.DATE_OF_BIRTH,
    IdentityStep.DISAMBIGUATING_DOB: AwaitedInput.DOB_DISAMBIGUATION,
    IdentityStep.CONFIRMING_DOB: AwaitedInput.DOB_CONFIRMATION,
    IdentityStep.AWAITING_SECOND_FACTOR: AwaitedInput.SECOND_FACTOR,
}

_STEP = "_identity.step"
#: What the caller has said, not yet agreed.
_NAME = "_identity.name"
_DOB = "_identity.dob"
_DOB_OPTIONS = "_identity.dob_options"
#: What they have agreed to. Only these are ever verified or registered.
_CONFIRMED_NAME = "_identity.confirmed_name"
_CONFIRMED_DOB = "_identity.confirmed_dob"
#: The pair from the last attempt that failed verification, kept only so that
#: a caller who then says they are new is not asked for it a third time.
_PREVIOUS = "_identity.previous"
_SPELLING = "_identity.spelling"
#: The part a spelling was just applied to, so letters arriving on the next
#: turn can be joined to it rather than replace it.
_JUST_SPELLED = "_identity.just_spelled"
_SPELLED = "_identity.spelled_parts"
_NAME_STRIKES = "_identity.name_strikes"
_DOB_STRIKES = "_identity.dob_strikes"

_COLLECTION_KEYS = (
    _STEP,
    _NAME,
    _DOB,
    _DOB_OPTIONS,
    _CONFIRMED_NAME,
    _CONFIRMED_DOB,
    _PREVIOUS,
    _SPELLING,
    _JUST_SPELLED,
    _SPELLED,
    _NAME_STRIKES,
    _DOB_STRIKES,
    _PARTIAL_NAME,
    _PARTIAL_DOB,
)


class IdentityOutcome(StrEnum):
    VERIFIED = "VERIFIED"
    NEEDS_IDENTITY = "NEEDS_IDENTITY"
    NEEDS_SECOND_FACTOR = "NEEDS_SECOND_FACTOR"
    LOCKED_OUT = "LOCKED_OUT"
    #: Name and date of birth both confirmed; nothing checked yet.
    COLLECTED = "COLLECTED"
    #: Three strikes on the name or the date of birth; a person takes over.
    HANDED_OVER = "HANDED_OVER"


class IdentityInput(BaseModel):
    """What this turn said that the identity steps can use."""

    model_config = ConfigDict(frozen=True)

    utterance: str = ""
    full_name: str | None = None
    spelled: str | None = None
    date_of_birth: date | None = None
    dob_candidates: tuple[date, ...] = ()
    confirm: bool | None = None
    second_factor_type: SecondFactorType | None = None
    second_factor_value: str | None = None
    #: The caller stated a request this turn, before giving a name.
    request_noted: bool = False

    @property
    def is_empty(self) -> bool:
        """Nothing in it that answers an identity question."""
        return (
            self.full_name is None
            and self.spelled is None
            and self.date_of_birth is None
            and not self.dob_candidates
            and self.confirm is None
        )


class IdentityResult(BaseModel):
    """What the workflow should do next about identity."""

    model_config = ConfigDict(frozen=True)

    outcome: IdentityOutcome
    message: str
    awaiting: AwaitedInput | None = None
    escalation_id: str | None = None
    step: IdentityStep | None = None
    #: For "Thanks, John" -- the caller's own first name, as they confirmed it.
    given_name: str | None = None

    @property
    def is_verified(self) -> bool:
        return self.outcome is IdentityOutcome.VERIFIED


class IdentityCollector:
    """Runs the identity steps and audits them."""

    def __init__(self, verification: VerificationService, audit: AuditService) -> None:
        self.verification = verification
        self.audit = audit

    @staticmethod
    def ask() -> IdentityResult:
        return IdentityResult(
            outcome=IdentityOutcome.NEEDS_IDENTITY,
            message=ASK_NAME,
            awaiting=AwaitedInput.NAME,
            step=IdentityStep.ASKING_NAME,
        )

    @staticmethod
    def ask_second_factor() -> IdentityResult:
        return IdentityResult(
            outcome=IdentityOutcome.NEEDS_SECOND_FACTOR,
            message="Could I take the last four digits of the phone number on file?",
            awaiting=AwaitedInput.SECOND_FACTOR,
            step=IdentityStep.AWAITING_SECOND_FACTOR,
        )

    # ========================================================== the steps
    @staticmethod
    def step(session: SessionState) -> IdentityStep:
        raw = session.workflow_state.get(_STEP)
        try:
            return IdentityStep(str(raw)) if raw is not None else IdentityStep.ASKING_NAME
        except ValueError:
            return IdentityStep.ASKING_NAME

    @staticmethod
    def has_started(session: SessionState) -> bool:
        """Whether anything identifying has been said yet this call."""
        state = session.workflow_state
        return any(
            state.get(key) is not None for key in (_NAME, _DOB, _DOB_OPTIONS, _CONFIRMED_NAME)
        )

    async def advance(self, session: SessionState, turn: IdentityInput) -> IdentityResult:
        """One turn of the full path: collect, confirm, verify."""
        if session.is_locked_out:
            return IdentityResult(
                outcome=IdentityOutcome.LOCKED_OUT, message=LOCKED_OUT, step=IdentityStep.LOCKED_OUT
            )

        if self.step(session) is IdentityStep.AWAITING_SECOND_FACTOR:
            result = await self.submit_second_factor(
                session, turn.second_factor_type, turn.second_factor_value
            )
            return self._after_verification(session, result)

        collected = self.collect(session, turn)
        if collected.outcome is not IdentityOutcome.COLLECTED:
            return collected

        confirmed = self.confirmed_identity(session, include_previous=False)
        assert confirmed is not None  # COLLECTED means both halves are confirmed
        name, born = confirmed
        result = await self.submit_identity(session, name.full, born)
        return self._after_verification(session, result, given=name.given)

    def collect(self, session: SessionState, turn: IdentityInput) -> IdentityResult:
        """Take and confirm a name and a date of birth. Verifies nothing."""
        if self._confirmed_name(session) is not None and self._confirmed_dob(session) is not None:
            return self._collected(session)

        match self.step(session):
            case IdentityStep.CONFIRMING_NAME:
                return self._confirm_name(session, turn)
            case IdentityStep.SPELLING_NAME:
                return self._take_spelling(session, turn)
            case IdentityStep.ASKING_DOB:
                return self._take_dob(session, turn)
            case IdentityStep.DISAMBIGUATING_DOB:
                return self._disambiguate(session, turn)
            case IdentityStep.CONFIRMING_DOB:
                return self._confirm_dob(session, turn)
            case _:
                if self._confirmed_name(session) is not None:
                    return self._take_dob(session, turn)
                return self._take_name(session, turn)

    def prompt(self, session: SessionState) -> IdentityResult:
        """The question outstanding at the current step, asked again.

        For a turn that was not an answer to it -- "I've never been here
        before" said while the date of birth was being asked for starts a
        registration, and counting it as a failed date would be wrong twice
        over: it is a strike the caller did not earn, and the reply would be
        "sorry, I didn't catch that" to something heard perfectly well.
        """
        step = self.step(session)
        held = self._held_name(session)
        confirmed = self._confirmed_name(session)
        if step is IdentityStep.CONFIRMING_NAME and held is not None and held.family:
            return self._spell_back(session, held)
        if step is IdentityStep.SPELLING_NAME and held is not None:
            part = str(session.workflow_state.get(_SPELLING) or "family")
            return self._ask(
                session,
                IdentityStep.SPELLING_NAME,
                f"Could you spell your {_PART_LABELS.get(part, 'last name')} for me?",
            )
        held_dob = _as_date(session.workflow_state.get(_DOB))
        if step is IdentityStep.CONFIRMING_DOB and held_dob is not None:
            return self._read_back_dob(session, held_dob)
        options = self._dob_options(session)
        if step is IdentityStep.DISAMBIGUATING_DOB and options:
            return self._ask_which(session, options)
        if confirmed is not None and self._confirmed_dob(session) is None:
            return self._ask(
                session,
                IdentityStep.ASKING_DOB,
                ASK_DATE_OF_BIRTH_AFTER_NAME.format(given=confirmed.given),
            )
        return self._ask(session, IdentityStep.ASKING_NAME, ASK_NAME)

    def confirmed_identity(
        self, session: SessionState, *, include_previous: bool = True
    ) -> tuple[NameParts, date] | None:
        """The name and date of birth the caller has agreed to, if both.

        ``include_previous`` falls back to the pair from an attempt that failed
        verification, but only while nothing new has been said since. A caller
        told "I couldn't find a match" who answers "I've never been before" has
        already confirmed both, letter by letter; asking again would be the
        agent not listening (ADR 009 still holds -- they had to say it).
        """
        name, born = self._confirmed_name(session), self._confirmed_dob(session)
        if name is not None and born is not None:
            return name, born
        if not include_previous or self.has_started(session):
            return None
        previous = session.workflow_state.get(_PREVIOUS)
        if not isinstance(previous, dict):
            return None
        old_name = NameParts.from_dict(previous.get("name"))
        old_dob = _as_date(previous.get("dob"))
        if old_name is None or old_dob is None:
            return None
        return old_name, old_dob

    @staticmethod
    def reject_date_of_birth(session: SessionState) -> None:
        """Take the date of birth again: the record refused the confirmed one."""
        for key in (_CONFIRMED_DOB, _DOB, _DOB_OPTIONS):
            session.workflow_state.pop(key, None)
        previous = session.workflow_state.pop(_PREVIOUS, None)
        if isinstance(previous, dict) and session.workflow_state.get(_CONFIRMED_NAME) is None:
            session.workflow_state[_CONFIRMED_NAME] = previous.get("name")
        session.workflow_state[_STEP] = IdentityStep.ASKING_DOB.value

    @staticmethod
    def forget(session: SessionState) -> None:
        """Drop everything identity collection was holding."""
        for key in _COLLECTION_KEYS:
            session.workflow_state.pop(key, None)

    # ---------------------------------------------------------------- name
    def _take_name(self, session: SessionState, turn: IdentityInput) -> IdentityResult:
        self._absorb_dob(session, turn)
        held = self._held_name(session)

        if turn.full_name is None:
            message = ASK_NAME_FOR_REQUEST if turn.request_noted else NAME_NOT_CAUGHT
            if held is not None:
                message = ASK_FAMILY_NAME.format(given=held.given)
            return self._ask(session, IdentityStep.ASKING_NAME, message)

        parts = split_name(turn.full_name)
        if parts.family is None:
            if held is None or held.family is not None:
                # Only a first name so far. Keep it and ask for the rest,
                # rather than asking for "first and last" all over again.
                self._hold_name(session, parts)
                return self._ask(
                    session, IdentityStep.ASKING_NAME, ASK_FAMILY_NAME.format(given=parts.given)
                )
            parts = NameParts(given=held.given, family=parts.given)

        self._hold_name(session, parts)
        return self._spell_back(session, parts)

    def _confirm_name(self, session: SessionState, turn: IdentityInput) -> IdentityResult:
        held = self._held_name(session)
        if held is None or held.family is None:
            return self._take_name(session, turn)

        just_spelled = session.workflow_state.pop(_JUST_SPELLED, None)
        if (
            isinstance(just_spelled, str)
            and turn.spelled is not None
            and turn.confirm is None
            and _labelled_part(turn.utterance.lower()) is None
        ):
            # Letters and nothing else, straight after a spelling was read
            # back: the rest of it, cut off by a pause. Joined on, not swapped
            # in, and not a strike -- the caller has not rejected anything.
            current = _part_of(held, just_spelled) or ""
            joined = _with_part(held, just_spelled, name_from_letters(current + turn.spelled))
            self._hold_name(session, joined)
            session.workflow_state[_JUST_SPELLED] = just_spelled
            return self._spell_back(session, joined)

        if turn.spelled is not None and turn.confirm is not True:
            # "No -- S M Y T H." A correction, and a strike against the read-back.
            part = self._part_for_letters(turn.utterance, turn.spelled, held)
            if sum(c.isalpha() for c in turn.spelled) < 2:
                # One letter is not a name; it was only allowed through to
                # finish a spelling. Ask for the part properly instead.
                return self._ask_to_spell(session, turn, held)
            corrected = _with_part(held, part, name_from_letters(turn.spelled))
            session.workflow_state[_JUST_SPELLED] = part
            return self._name_corrected(session, corrected, part)

        if turn.confirm is True:
            return self._name_confirmed(session, held)

        if turn.full_name is not None:
            said = split_name(turn.full_name)
            if said.family is None and _only_says(turn.utterance, said.given):
                # One word and nothing else: "No, Smyth." Whichever part it is
                # closest to. With anything more -- "Jon without the h" -- the
                # word is a clue to which part, not the answer; that falls
                # through to asking for a spelling below.
                part = self._part_for_letters(turn.utterance, said.given.upper(), held)
                return self._name_corrected(session, _with_part(held, part, said.given), part)
            if said.family is not None and said != held:
                return self._name_corrected(session, said, None)

        if turn.confirm is False or turn.full_name is not None:
            return self._ask_to_spell(session, turn, held)

        # Neither yes nor no. Read it back again rather than guess.
        return self._spell_back(session, held)

    def _ask_to_spell(
        self, session: SessionState, turn: IdentityInput, held: NameParts
    ) -> IdentityResult:
        if self._strike(session, _NAME_STRIKES, "name"):
            return self._hand_over(session, "name")
        part = self._part_mentioned(turn.utterance, held) or self._next_to_spell(session)
        session.workflow_state[_SPELLING] = part
        return self._ask(
            session, IdentityStep.SPELLING_NAME, ASK_SPELLING.format(part=_PART_LABELS[part])
        )

    def _take_spelling(self, session: SessionState, turn: IdentityInput) -> IdentityResult:
        held = self._held_name(session)
        # What was asked for, unless the caller said which part they spelled.
        part = _labelled_part(turn.utterance.lower()) or str(
            session.workflow_state.get(_SPELLING) or "family"
        )
        if held is None:
            return self._take_name(session, turn)

        if turn.spelled is None:
            if self._strike(session, _NAME_STRIKES, "name"):
                return self._hand_over(session, "name")
            return self._ask(
                session,
                IdentityStep.SPELLING_NAME,
                SPELLING_NOT_CAUGHT.format(part=_PART_LABELS.get(part, "last name")),
            )

        self._mark_spelled(session, part)
        updated = _with_part(held, part, name_from_letters(turn.spelled))
        self._hold_name(session, updated)
        session.workflow_state[_JUST_SPELLED] = part
        return self._spell_back(session, updated)

    def _name_corrected(
        self, session: SessionState, corrected: NameParts, part: str | None
    ) -> IdentityResult:
        if self._strike(session, _NAME_STRIKES, "name"):
            return self._hand_over(session, "name")
        if part is not None:
            self._mark_spelled(session, part)
        self._hold_name(session, corrected)
        return self._spell_back(session, corrected)

    def _name_confirmed(self, session: SessionState, held: NameParts) -> IdentityResult:
        state = session.workflow_state
        state[_CONFIRMED_NAME] = held.as_dict()
        for key in (_NAME, _SPELLING, _SPELLED):
            state.pop(key, None)
        logger.info("identity_name_confirmed", session_id=session.session_id)

        thanks = f"Thank you, {held.given}."
        born = _as_date(state.get(_DOB))
        if born is not None:
            return self._read_back_dob(session, born, prefix=thanks)
        options = self._dob_options(session)
        if options:
            return self._ask_which(session, options, prefix=thanks)
        if self._confirmed_dob(session) is not None:
            return self._collected(session)
        return self._ask(
            session, IdentityStep.ASKING_DOB, ASK_DATE_OF_BIRTH_AFTER_NAME.format(given=held.given)
        )

    def _spell_back(self, session: SessionState, parts: NameParts) -> IdentityResult:
        """ "I have your first name as J-O-H-N and your last name as S-M-I-T-H."

        Each part is spelled separately and named, because "J-O-H-N-S-M-I-T-H"
        is unreadable and leaves the caller to work out where the break was.
        """
        pieces = [f"your first name as {spell_written(parts.given)}"]
        if parts.middle:
            pieces.append(f"your middle name as {spell_written(parts.middle)}")
        pieces.append(f"your last name as {spell_written(parts.family or '')}")
        listed = pieces[0] if len(pieces) == 1 else ", ".join(pieces[:-1]) + " and " + pieces[-1]
        return self._ask(
            session, IdentityStep.CONFIRMING_NAME, f"Thanks. I have {listed}. Is that right?"
        )

    # ------------------------------------------------------- date of birth
    def _take_dob(self, session: SessionState, turn: IdentityInput) -> IdentityResult:
        if turn.date_of_birth is not None:
            return self._read_back_dob(session, turn.date_of_birth)
        if len(turn.dob_candidates) == 2:
            return self._ask_which(session, turn.dob_candidates)
        # Nothing usable -- including three or more readings, which no "X or
        # Y?" question can settle.
        if self._strike(session, _DOB_STRIKES, "date_of_birth"):
            return self._hand_over(session, "date_of_birth")
        return self._ask(session, IdentityStep.ASKING_DOB, DOB_NOT_CAUGHT)

    def _confirm_dob(self, session: SessionState, turn: IdentityInput) -> IdentityResult:
        held = _as_date(session.workflow_state.get(_DOB))
        if held is None:
            return self._take_dob(session, turn)

        if turn.date_of_birth is not None and turn.date_of_birth != held:
            # "No, the sixteenth of February nineteen eighty five."
            if self._strike(session, _DOB_STRIKES, "date_of_birth"):
                return self._hand_over(session, "date_of_birth")
            return self._read_back_dob(session, turn.date_of_birth)

        if turn.confirm is True:
            session.workflow_state[_CONFIRMED_DOB] = held.isoformat()
            session.workflow_state.pop(_DOB, None)
            logger.info("identity_dob_confirmed", session_id=session.session_id)
            if self._confirmed_name(session) is None:
                return self._take_name(session, IdentityInput())
            return self._collected(session)

        if turn.confirm is False:
            # Never reuse the rejected value. Whatever was misheard the first
            # time would only be misheard the same way again.
            session.workflow_state.pop(_DOB, None)
            if self._strike(session, _DOB_STRIKES, "date_of_birth"):
                return self._hand_over(session, "date_of_birth")
            if len(turn.dob_candidates) == 2:
                return self._ask_which(session, turn.dob_candidates)
            return self._ask(session, IdentityStep.ASKING_DOB, ASK_DOB_AGAIN)

        return self._read_back_dob(session, held)

    def _disambiguate(self, session: SessionState, turn: IdentityInput) -> IdentityResult:
        options = self._dob_options(session)
        if len(options) != 2:
            return self._take_dob(session, turn)

        chosen = _choose(turn, options)
        if chosen is not None:
            session.workflow_state.pop(_DOB_OPTIONS, None)
            return self._read_back_dob(session, chosen)

        if self._strike(session, _DOB_STRIKES, "date_of_birth"):
            return self._hand_over(session, "date_of_birth")
        return self._ask_which(session, options)

    def _read_back_dob(self, session: SessionState, born: date, prefix: str = "") -> IdentityResult:
        """Always read back, with the month as a word.

        Even a date that parsed with no ambiguity at all. The parser knows
        which date it heard; only the caller knows whether that is the date
        they said.
        """
        session.workflow_state[_DOB] = born.isoformat()
        session.workflow_state.pop(_DOB_OPTIONS, None)
        message = CONFIRM_DOB.format(date=speak_date(born))
        return self._ask(session, IdentityStep.CONFIRMING_DOB, f"{prefix} {message}".strip())

    def _ask_which(
        self, session: SessionState, options: tuple[date, ...], prefix: str = ""
    ) -> IdentityResult:
        first, second = sorted(options)
        session.workflow_state[_DOB_OPTIONS] = [first.isoformat(), second.isoformat()]
        session.workflow_state.pop(_DOB, None)
        message = DISAMBIGUATE_DOB.format(
            first=speak_month_day(first), second=speak_month_day(second)
        )
        return self._ask(session, IdentityStep.DISAMBIGUATING_DOB, f"{prefix} {message}".strip())

    def _absorb_dob(self, session: SessionState, turn: IdentityInput) -> None:
        """A date of birth said with the name is kept, unconfirmed, for later."""
        if turn.date_of_birth is not None:
            session.workflow_state[_DOB] = turn.date_of_birth.isoformat()
            session.workflow_state.pop(_DOB_OPTIONS, None)
        elif len(turn.dob_candidates) == 2:
            session.workflow_state[_DOB_OPTIONS] = [
                d.isoformat() for d in sorted(turn.dob_candidates)
            ]
            session.workflow_state.pop(_DOB, None)

    # ------------------------------------------------------- verification
    def _after_verification(
        self, session: SessionState, result: IdentityResult, given: str | None = None
    ) -> IdentityResult:
        """Move the steps on from what verification decided."""
        state = session.workflow_state
        match result.outcome:
            case IdentityOutcome.VERIFIED:
                given = given or _given_from(state.get(_CONFIRMED_NAME))
                self.forget(session)
                state[_STEP] = IdentityStep.VERIFIED.value
                return result.model_copy(
                    update={
                        "message": VERIFIED.format(given=given) if given else "",
                        "step": IdentityStep.VERIFIED,
                        "given_name": given,
                    }
                )
            case IdentityOutcome.NEEDS_SECOND_FACTOR:
                state[_STEP] = IdentityStep.AWAITING_SECOND_FACTOR.value
                return result.model_copy(update={"step": IdentityStep.AWAITING_SECOND_FACTOR})
            case IdentityOutcome.LOCKED_OUT:
                self.forget(session)
                state[_STEP] = IdentityStep.LOCKED_OUT.value
                return result.model_copy(update={"step": IdentityStep.LOCKED_OUT})
            case _:
                # No match. Start again from the name -- the same steps as the
                # first time -- and keep the failed pair aside only for a
                # caller who now says they are new.
                previous = {
                    "name": state.get(_CONFIRMED_NAME),
                    "dob": state.get(_CONFIRMED_DOB),
                }
                self.forget(session)
                state[_PREVIOUS] = previous
                state[_STEP] = IdentityStep.ASKING_NAME.value
                return result.model_copy(
                    update={"step": IdentityStep.ASKING_NAME, "awaiting": AwaitedInput.NAME}
                )

    async def submit_identity(
        self, session: SessionState, full_name: str | None, date_of_birth: date | None
    ) -> IdentityResult:
        """Verify, remembering whichever half of the identity has arrived.

        The typed entry point: a workflow handed a name and a date of birth
        that are already confirmed, and the last step of ``advance``. Every
        verification goes through here, which is what keeps the reply to a
        failure identical wherever it happens.

        Identity used to require both facts in a single utterance, and anything
        given on its own was silently discarded -- so a caller who answered
        "John Smith", then gave their date of birth, was asked for both again
        and again. Every test in the suite passed because every test said
        "My name is X and I was born Y" in one breath, which is not how anyone
        speaks, least of all on the phone.
        """
        full_name = full_name or self._remembered_name(session)
        date_of_birth = date_of_birth or self._remembered_dob(session)

        if full_name is None or date_of_birth is None:
            self._remember(session, full_name, date_of_birth)
            if full_name is not None:
                return self._typed_ask(ASK_DATE_OF_BIRTH, AwaitedInput.DATE_OF_BIRTH)
            if date_of_birth is not None:
                return self._typed_ask(ASK_FULL_NAME, AwaitedInput.NAME)
            return self.ask()

        # A complete attempt is about to be made, so the partials have served
        # their purpose. Clearing them here means a failed attempt starts the
        # next one clean rather than silently reusing a value the caller was
        # trying to correct.
        session.workflow_state.pop(_PARTIAL_NAME, None)
        session.workflow_state.pop(_PARTIAL_DOB, None)

        self.audit.record(AuditAction.VERIFICATION_ATTEMPTED, session_id=session.session_id)
        result = await self.verification.verify_identity(session, full_name, date_of_birth)

        if result.outcome is VerificationOutcome.VERIFIED:
            return self._verified(session)

        if result.outcome is VerificationOutcome.SECOND_FACTOR_REQUIRED:
            return IdentityResult(
                outcome=IdentityOutcome.NEEDS_SECOND_FACTOR,
                message=ASK_SECOND_FACTOR,
                awaiting=AwaitedInput.SECOND_FACTOR,
            )

        self._record_failure(session)
        if result.outcome in (
            VerificationOutcome.LOCKED_OUT,
            VerificationOutcome.ALREADY_LOCKED,
        ):
            return IdentityResult(
                outcome=IdentityOutcome.LOCKED_OUT,
                message=LOCKED_OUT,
                escalation_id=result.escalation_id,
            )

        return IdentityResult(
            outcome=IdentityOutcome.NEEDS_IDENTITY,
            message=RETRY_IDENTITY,
            awaiting=AwaitedInput.NAME,
        )

    async def submit_second_factor(
        self,
        session: SessionState,
        factor_type: SecondFactorType | None,
        value: str | None,
    ) -> IdentityResult:
        if value is None:
            return self.ask_second_factor()

        result = await self.verification.submit_second_factor(
            session, factor_type or SecondFactorType.PHONE_LAST_FOUR, value
        )

        if result.outcome is VerificationOutcome.VERIFIED:
            return self._verified(session)

        self._record_failure(session)
        if result.outcome in (
            VerificationOutcome.LOCKED_OUT,
            VerificationOutcome.ALREADY_LOCKED,
        ):
            return IdentityResult(
                outcome=IdentityOutcome.LOCKED_OUT,
                message=LOCKED_OUT,
                escalation_id=result.escalation_id,
            )

        return IdentityResult(
            outcome=IdentityOutcome.NEEDS_SECOND_FACTOR,
            message=RETRY_SECOND_FACTOR,
            awaiting=AwaitedInput.SECOND_FACTOR,
        )

    def _verified(self, session: SessionState) -> IdentityResult:
        self.audit.record(
            AuditAction.VERIFICATION_SUCCEEDED,
            session_id=session.session_id,
            patient_ref=session.patient_ref,
        )
        return IdentityResult(outcome=IdentityOutcome.VERIFIED, message="")

    def _record_failure(self, session: SessionState) -> None:
        self.audit.record(
            AuditAction.VERIFICATION_FAILED,
            session_id=session.session_id,
            outcome="denied",
        )

    # ----------------------------------------------------------- handover
    def _strike(self, session: SessionState, key: str, half: str) -> bool:
        """Count a read-back the caller rejected, or an answer nobody could use.

        Audited, by half and count only -- never the value that was wrong.
        True when this one is the last allowed.
        """
        seen = session.workflow_state.get(key, 0)
        strikes = (seen if isinstance(seen, int) else 0) + 1
        session.workflow_state[key] = strikes
        self.audit.record(
            AuditAction.IDENTITY_NOT_CONFIRMED,
            session_id=session.session_id,
            outcome="failure",
            detail=f"{half}, attempt {strikes} of {MAX_STRIKES}",
        )
        logger.info(
            "identity_not_confirmed", session_id=session.session_id, half=half, strikes=strikes
        )
        return strikes >= MAX_STRIKES

    def _hand_over(self, session: SessionState, half: str) -> IdentityResult:
        """Three strikes: a person takes the call, with the reason attached."""
        what = "name" if half == "name" else "date of birth"
        escalation = self.verification.escalations.create(
            category=EscalationCategory.SYSTEM_UNCERTAINTY,
            summary=(
                f"Caller's {what} could not be confirmed after {MAX_STRIKES} attempts. "
                "Nothing was checked against the record and no patient information "
                "was disclosed."
            ),
            session_id=session.session_id,
            verification_state=session.verification,
            ai_action="Handed to the front desk before verification",
        )
        self.audit.record(
            AuditAction.ESCALATION_CREATED,
            session_id=session.session_id,
            resource_type="Escalation",
            resource_id=escalation.escalation_id,
            detail=f"{half} not confirmed",
        )
        self.forget(session)
        session.workflow_state[_STEP] = IdentityStep.HANDED_OVER.value
        return IdentityResult(
            outcome=IdentityOutcome.HANDED_OVER,
            message=IDENTITY_HANDOVER,
            escalation_id=escalation.escalation_id,
            step=IdentityStep.HANDED_OVER,
        )

    # -------------------------------------------------------------- memory
    def _ask(self, session: SessionState, step: IdentityStep, message: str) -> IdentityResult:
        session.workflow_state[_STEP] = step.value
        return IdentityResult(
            outcome=IdentityOutcome.NEEDS_IDENTITY,
            message=message,
            awaiting=_AWAITING[step],
            step=step,
        )

    def _collected(self, session: SessionState) -> IdentityResult:
        name = self._confirmed_name(session)
        return IdentityResult(
            outcome=IdentityOutcome.COLLECTED,
            message="",
            step=self.step(session),
            given_name=name.given if name else None,
        )

    @staticmethod
    def _typed_ask(message: str, awaiting: AwaitedInput) -> IdentityResult:
        return IdentityResult(
            outcome=IdentityOutcome.NEEDS_IDENTITY,
            message=message,
            awaiting=awaiting,
        )

    @staticmethod
    def _held_name(session: SessionState) -> NameParts | None:
        return NameParts.from_dict(session.workflow_state.get(_NAME))

    @staticmethod
    def _hold_name(session: SessionState, parts: NameParts) -> None:
        session.workflow_state[_NAME] = parts.as_dict()

    @staticmethod
    def _confirmed_name(session: SessionState) -> NameParts | None:
        return NameParts.from_dict(session.workflow_state.get(_CONFIRMED_NAME))

    @staticmethod
    def _confirmed_dob(session: SessionState) -> date | None:
        return _as_date(session.workflow_state.get(_CONFIRMED_DOB))

    @staticmethod
    def _dob_options(session: SessionState) -> tuple[date, ...]:
        raw = session.workflow_state.get(_DOB_OPTIONS)
        if not isinstance(raw, list):
            return ()
        parsed = tuple(d for d in (_as_date(item) for item in raw) if d is not None)
        return parsed if len(parsed) == 2 else ()

    @staticmethod
    def _mark_spelled(session: SessionState, part: str) -> None:
        raw = session.workflow_state.get(_SPELLED)
        spelled = list(raw) if isinstance(raw, list) else []
        if part not in spelled:
            spelled.append(part)
        session.workflow_state[_SPELLED] = spelled

    @staticmethod
    def _next_to_spell(session: SessionState) -> str:
        """Last name first -- the half recognisers get wrong -- then the first."""
        raw = session.workflow_state.get(_SPELLED)
        spelled = raw if isinstance(raw, list) else []
        return "given" if "family" in spelled and "given" not in spelled else "family"

    @staticmethod
    def _part_mentioned(utterance: str, held: NameParts) -> str | None:
        """Which part a free-form correction is about, if it says.

        "No, it's Jon without the h" names no part, but "Jon" is plainly the
        first name. Nothing here invents letters: it only decides which part to
        ask the caller to spell.
        """
        lowered = utterance.lower()
        labelled = _labelled_part(lowered)
        if labelled is not None:
            return labelled
        best: tuple[float, str] | None = None
        for word in re.findall(r"[a-z']+", lowered):
            for part, value in (("given", held.given), ("family", held.family)):
                if not value or len(word) < 2:
                    continue
                ratio = difflib.SequenceMatcher(None, word, value.lower()).ratio()
                if ratio >= 0.6 and (best is None or ratio > best[0]):
                    best = (ratio, part)
        return best[1] if best else None

    @staticmethod
    def _part_for_letters(utterance: str, letters: str, held: NameParts) -> str:
        """Which part a spelled correction replaces.

        Said, if the caller said ("my last name is ..."); otherwise whichever
        part the letters most resemble, because a correction is nearly always
        a near miss; otherwise the last name, which is the one recognisers
        mangle.
        """
        labelled = _labelled_part(utterance.lower())
        if labelled is not None:
            return labelled
        target = re.sub(r"[^A-Z]", "", letters.upper())
        scores = {
            part: difflib.SequenceMatcher(
                None, target, re.sub(r"[^A-Z]", "", value.upper())
            ).ratio()
            for part, value in (("given", held.given), ("family", held.family or ""))
        }
        if scores["given"] > scores["family"] and scores["given"] >= 0.5:
            return "given"
        return "family"

    # ---------------------------------------------------- typed partials
    @staticmethod
    def _remember(session: SessionState, full_name: str | None, date_of_birth: date | None) -> None:
        if full_name is not None:
            session.workflow_state[_PARTIAL_NAME] = full_name
        if date_of_birth is not None:
            session.workflow_state[_PARTIAL_DOB] = date_of_birth.isoformat()

    @staticmethod
    def _remembered_name(session: SessionState) -> str | None:
        value = session.workflow_state.get(_PARTIAL_NAME)
        return value if isinstance(value, str) else None

    @staticmethod
    def _remembered_dob(session: SessionState) -> date | None:
        return _as_date(session.workflow_state.get(_PARTIAL_DOB))


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _as_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _given_from(raw: object) -> str | None:
    parts = NameParts.from_dict(raw)
    return parts.given if parts else None


def _with_part(held: NameParts, part: str, value: str) -> NameParts:
    if part == "given":
        return NameParts(given=value, middle=held.middle, family=held.family)
    if part == "middle":
        return NameParts(given=held.given, middle=value, family=held.family)
    return NameParts(given=held.given, middle=held.middle, family=value)


#: Words around a one-word correction that do not change what it says.
_CORRECTION_GLUE = frozenset(
    {
        "no",
        "nope",
        "sorry",
        "it's",
        "its",
        "it",
        "is",
        "my",
        "name",
        "name's",
        "first",
        "last",
        "middle",
        "surname",
        "the",
        "that's",
        "actually",
        "um",
        "uh",
        "oh",
        "should",
        "be",
        "i",
        "said",
    }
)


def _only_says(utterance: str, word: str) -> bool:
    """Whether a correction is the bare word, give or take "no, it's"."""
    words = [w for w in re.findall(r"[a-z']+", utterance.lower()) if w not in _CORRECTION_GLUE]
    return words == [word.lower()]


def _part_of(held: NameParts, part: str) -> str | None:
    return {"given": held.given, "middle": held.middle, "family": held.family}.get(part)


def _labelled_part(lowered: str) -> str | None:
    if re.search(r"\b(?:last name|surname|family name|second name)\b", lowered):
        return "family"
    if re.search(r"\b(?:first name|given name|christian name|forename)\b", lowered):
        return "given"
    if re.search(r"\bmiddle name\b", lowered):
        return "middle"
    return None


def _choose(turn: IdentityInput, options: tuple[date, ...]) -> date | None:
    """Which of two readings the caller meant, from how they answered.

    By the date itself, by naming the month ("March"), or by position ("the
    first one"). A month that both readings share settles nothing, and
    nothing is guessed.
    """
    if turn.date_of_birth is not None:
        return turn.date_of_birth
    lowered = turn.utterance.lower()
    named = [
        option
        for option in options
        if re.search(rf"\b{MONTH_NAMES[option.month - 1].lower()[:3]}[a-z]*\b", lowered)
    ]
    if len(named) == 1:
        return named[0]
    if re.search(r"\b(?:first|former|1st)\b", lowered):
        return options[0]
    if re.search(r"\b(?:second|latter|last|2nd)\b", lowered):
        return options[1]
    return None
