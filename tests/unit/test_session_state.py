"""Session state: the properties that make verification non-forgeable."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.agents.state import SessionChannel, SessionState, VerificationDecision
from app.schemas.domain import VerificationState
from app.services.session_service import SessionStore

VERIFIED = VerificationDecision(
    state=VerificationState.VERIFIED, patient_ref="Patient/demo-john-smith"
)


def test_a_new_session_is_unverified() -> None:
    session = SessionState("sess-1")
    assert session.verification is VerificationState.UNVERIFIED
    assert session.patient_ref is None
    assert session.is_verified is False


def test_verification_state_cannot_be_assigned_directly() -> None:
    """The whole gate would be worthless if conversation code could set this."""
    session = SessionState("sess-1")
    with pytest.raises(AttributeError):
        session.patient_ref = "Patient/demo-john-smith"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        session.verification = VerificationState.VERIFIED  # type: ignore[misc]
    with pytest.raises(AttributeError):
        session.is_verified = True  # type: ignore[misc]


def test_applying_a_decision_is_the_only_way_in() -> None:
    session = SessionState("sess-1")
    session.apply_verification(VERIFIED)
    assert session.is_verified is True
    assert session.patient_ref == "Patient/demo-john-smith"


def test_any_non_verified_outcome_revokes_access() -> None:
    session = SessionState("sess-1")
    session.apply_verification(VERIFIED)
    session.apply_verification(VerificationDecision(state=VerificationState.UNVERIFIED))
    assert session.patient_ref is None
    assert session.is_verified is False


def test_workflow_state_carries_no_authority() -> None:
    """Scratch space is freely mutable; that must not grant access."""
    session = SessionState("sess-1")
    session.workflow_state["patient_ref"] = "Patient/demo-john-smith"
    session.workflow_state["verified"] = True
    assert session.is_verified is False
    assert session.patient_ref is None


def test_ending_a_session_revokes_verification() -> None:
    """Verification is scoped to the session (ADR 003)."""
    session = SessionState("sess-1")
    session.apply_verification(VERIFIED)
    session.end()
    assert session.is_verified is False
    assert session.patient_ref is None
    assert session.is_active is False


def test_snapshot_exposes_a_reference_but_no_candidates() -> None:
    session = SessionState("sess-1", channel=SessionChannel.VOICE)
    session.apply_verification(
        VerificationDecision(
            state=VerificationState.PENDING_SECOND_FACTOR,
            candidate_refs=("Patient/a", "Patient/b"),
        )
    )
    snapshot = session.snapshot()
    assert snapshot.channel is SessionChannel.VOICE
    assert snapshot.patient_ref is None
    assert "candidate" not in snapshot.model_dump_json()


class TestSessionStore:
    def test_sessions_round_trip(self) -> None:
        store = SessionStore()
        created = store.create()
        assert store.get(created.session_id) is created

    def test_unknown_session_raises(self) -> None:
        from app.services.base import NotFoundError

        with pytest.raises(NotFoundError):
            SessionStore().get("sess-nope")

    def test_stale_sessions_expire_and_lose_verification(self) -> None:
        """A verified session left open must not be inherited by a later caller."""
        start = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
        store = SessionStore(ttl=timedelta(minutes=30))
        session = store.create(now=start)
        session.apply_verification(VERIFIED)

        fetched = store.get(session.session_id, now=start + timedelta(minutes=31))
        assert fetched.is_active is False
        assert fetched.is_verified is False

    def test_active_sessions_exclude_ended_ones(self) -> None:
        store = SessionStore()
        first = store.create()
        store.create()
        store.end(first.session_id)
        assert [s.session_id for s in store.active_sessions()] != [first.session_id]
        assert len(store.active_sessions()) == 1
