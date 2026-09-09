"""Session lifecycle.

Sessions live in this process. Phase 11 moves them to the ``session`` table;
the interface here is what callers depend on.

Verification is scoped to a session (ADR 003), so ending one revokes access --
which is why expiry is enforced here rather than left to a caller to remember.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.agents.state import SessionChannel, SessionState
from app.observability.logging import get_logger
from app.services.base import NotFoundError

logger = get_logger(__name__)

#: A patient-access call that has been idle this long is over. Keeping a
#: verified session alive indefinitely would let a later caller inherit it.
DEFAULT_SESSION_TTL = timedelta(minutes=30)


class SessionStore:
    """In-process session registry."""

    def __init__(self, ttl: timedelta = DEFAULT_SESSION_TTL) -> None:
        self._sessions: dict[str, SessionState] = {}
        self.ttl = ttl

    def create(
        self,
        channel: SessionChannel = SessionChannel.TEXT,
        session_id: str | None = None,
        now: datetime | None = None,
    ) -> SessionState:
        identifier = session_id or f"sess-{uuid.uuid4().hex[:12]}"
        session = SessionState(
            session_id=identifier, channel=channel, created_at=now or datetime.now(UTC)
        )
        self._sessions[identifier] = session
        logger.info("session_created", session_id=identifier, channel=channel.value)
        return session

    def get(self, session_id: str, now: datetime | None = None) -> SessionState:
        """Fetch a session, expiring it first if it has gone stale."""
        session = self._sessions.get(session_id)
        if session is None:
            raise NotFoundError(f"no session {session_id!r}")
        self._expire_if_stale(session, now)
        return session

    def end(self, session_id: str, now: datetime | None = None) -> SessionState:
        session = self.get(session_id, now=now)
        if session.is_active:
            session.end(now)
            logger.info("session_ended", session_id=session_id)
        return session

    def active_sessions(self) -> list[SessionState]:
        return [s for s in self._sessions.values() if s.is_active]

    def clear(self) -> None:
        self._sessions.clear()

    def _expire_if_stale(self, session: SessionState, now: datetime | None) -> None:
        moment = now or datetime.now(UTC)
        if session.is_active and moment - session.created_at > self.ttl:
            session.end(moment)
            logger.info("session_expired", session_id=session.session_id)
