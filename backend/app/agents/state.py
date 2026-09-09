"""Conversation session state.

State lives server-side, in this process, and is mutated only through the
methods below. That is the point: the caller controls the entire text channel,
so anything derived from what they say is attacker-controlled. Verification in
particular is never inferred from conversation content (ADR 003).

``patient_ref`` and ``verification`` are read-only properties. The only way to
set them is :meth:`SessionState.apply_verification`, which requires a
``VerificationDecision`` -- a value produced by the verification service after
it has actually matched a record.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.schemas.domain import VerificationState


class SessionChannel(StrEnum):
    TEXT = "text"
    VOICE = "voice"
    PHONE = "phone"


class VerificationDecision(BaseModel):
    """The verification service's ruling on a session.

    Constructed only by ``VerificationService``. It is the sole key that opens
    :meth:`SessionState.apply_verification`, so every change of verification
    state has a single, auditable origin.
    """

    model_config = ConfigDict(frozen=True)

    state: VerificationState
    #: Set only when ``state`` is VERIFIED.
    patient_ref: str | None = None
    #: Candidate references held while a second factor is outstanding. Never
    #: exposed to the caller -- knowing that two records matched is itself a
    #: disclosure.
    candidate_refs: tuple[str, ...] = ()
    #: Internal explanation for the audit trail; never spoken to the patient.
    rationale: str = ""


class SessionSnapshot(BaseModel):
    """A safe view of a session for APIs, logs, and the dashboard.

    Carries the patient *reference* only. No demographics, and no candidate
    references.
    """

    model_config = ConfigDict(frozen=True)

    session_id: str
    channel: SessionChannel
    verification: VerificationState
    patient_ref: str | None
    failed_attempts: int
    turn_count: int
    is_verified: bool
    is_locked_out: bool
    created_at: datetime
    ended_at: datetime | None


class SessionState:
    """One conversation."""

    def __init__(
        self,
        session_id: str,
        channel: SessionChannel = SessionChannel.TEXT,
        created_at: datetime | None = None,
    ) -> None:
        self.session_id = session_id
        self.channel = channel
        self.created_at = created_at or datetime.now(UTC)
        self.ended_at: datetime | None = None

        # Deliberately private: exposed through read-only properties so that
        # `session.patient_ref = "Patient/someone-else"` raises AttributeError.
        self._verification = VerificationState.UNVERIFIED
        self._patient_ref: str | None = None
        self._candidate_refs: tuple[str, ...] = ()
        self._failed_attempts = 0
        self._turn_count = 0

        #: Workflow scratch space (offered slots, pending confirmation). Safe to
        #: mutate freely; it never carries authorisation.
        self.workflow_state: dict[str, object] = {}

        #: Which workflow is mid-conversation, so a follow-up turn resumes it
        #: rather than starting over. Carries no authority either.
        self.active_workflow: str | None = None

    # ------------------------------------------------------------ read-only
    @property
    def verification(self) -> VerificationState:
        return self._verification

    @property
    def patient_ref(self) -> str | None:
        """The verified patient, or None. Never set outside verification."""
        return self._patient_ref

    @property
    def candidate_refs(self) -> tuple[str, ...]:
        return self._candidate_refs

    @property
    def failed_attempts(self) -> int:
        return self._failed_attempts

    @property
    def turn_count(self) -> int:
        return self._turn_count

    @property
    def is_verified(self) -> bool:
        return (
            self._verification is VerificationState.VERIFIED
            and self._patient_ref is not None
            and self.ended_at is None
        )

    @property
    def is_locked_out(self) -> bool:
        return self._verification is VerificationState.FAILED

    @property
    def is_active(self) -> bool:
        return self.ended_at is None

    # ------------------------------------------------------------- mutators
    def apply_verification(self, decision: VerificationDecision) -> None:
        """Apply a ruling from the verification service.

        The only path that changes verification state.
        """
        self._verification = decision.state
        if decision.state is VerificationState.VERIFIED:
            self._patient_ref = decision.patient_ref
            self._candidate_refs = ()
        else:
            # Any non-verified outcome revokes access immediately.
            self._patient_ref = None
            self._candidate_refs = decision.candidate_refs

    def record_failed_attempt(self) -> int:
        self._failed_attempts += 1
        return self._failed_attempts

    def record_turn(self) -> int:
        self._turn_count += 1
        return self._turn_count

    def end(self, when: datetime | None = None) -> None:
        """Close the session.

        Verification is scoped to the session, so ending it revokes access.
        """
        self.ended_at = when or datetime.now(UTC)
        self._patient_ref = None
        self._candidate_refs = ()

    # -------------------------------------------------------------- viewing
    def snapshot(self) -> SessionSnapshot:
        return SessionSnapshot(
            session_id=self.session_id,
            channel=self.channel,
            verification=self._verification,
            patient_ref=self._patient_ref,
            failed_attempts=self._failed_attempts,
            turn_count=self._turn_count,
            is_verified=self.is_verified,
            is_locked_out=self.is_locked_out,
            created_at=self.created_at,
            ended_at=self.ended_at,
        )

    def __repr__(self) -> str:
        return (
            f"SessionState(session_id={self.session_id!r}, "
            f"verification={self._verification.value}, "
            f"patient_ref={self._patient_ref!r})"
        )
