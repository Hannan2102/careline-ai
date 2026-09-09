"""Persisting a turn.

**A turn is the unit of work.** The in-memory stores are the working set during
a turn; at the end of it, everything that turn produced is written in one
transaction: the session's current state, the trace, the audit events, any
escalations, refill requests, and metered usage.

Why a single flush rather than a write per event: the domain services are
synchronous and are called from deep inside workflows. Making every
``audit.record`` an ``await`` would push async plumbing through the safety
layer and the verification service for no benefit the demo can measure. One
explicit boundary is easier to reason about and easier to test.

The cost is real and worth stating: a crash mid-turn loses that turn's rows.
For an audit trail in a regulated setting that would not be acceptable, and
the fix is to write audit events synchronously at the point of access -- noted
in PROJECT_STATUS as a gap rather than glossed over.
"""

from __future__ import annotations

from app.agents.state import SessionState
from app.agents.trace import TurnTrace
from app.ai.usage import UsageLedger
from app.db.engine import Database
from app.db.repositories import (
    insert_audit_events,
    insert_escalations,
    insert_turn,
    insert_usage,
    upsert_refill_requests,
    upsert_session,
)
from app.observability.logging import get_logger
from app.services.audit_service import AuditService
from app.services.escalation_service import EscalationService
from app.services.refill_service import RefillService

logger = get_logger(__name__)


class PersistenceService:
    """Writes what a turn produced."""

    def __init__(
        self,
        database: Database,
        audit: AuditService,
        escalations: EscalationService,
        refills: RefillService,
        ledger: UsageLedger | None = None,
    ) -> None:
        self.database = database
        self.audit = audit
        self.escalations = escalations
        self.refills = refills
        self.ledger = ledger
        # High-water marks into the append-only in-memory stores, so each flush
        # writes only what is new.
        self._audit_mark = 0
        self._escalation_mark = 0
        self._refill_mark = 0
        self._usage_mark = 0

    async def flush_turn(self, session: SessionState, trace: TurnTrace) -> None:
        """Write everything produced since the last flush."""
        audit_events = self.audit.store.all()[self._audit_mark :]
        escalations = self.escalations.store.all()[self._escalation_mark :]
        refills = self.refills.store.all()[self._refill_mark :]
        usage = self.ledger.records[self._usage_mark :] if self.ledger is not None else []

        try:
            async with self.database.session() as db:
                await upsert_session(db, session)
                await insert_turn(db, trace)
                await insert_audit_events(db, audit_events, turn_id=trace.turn_id)
                await insert_escalations(db, escalations)
                await upsert_refill_requests(db, refills)
                await insert_usage(db, usage)
        except Exception as exc:
            # Persistence must not break the conversation: the caller is on the
            # phone. Log loudly and carry on with the in-memory working set.
            logger.error(
                "turn_persistence_failed",
                session_id=session.session_id,
                turn_id=trace.turn_id,
                error=str(exc),
            )
            return

        self._audit_mark += len(audit_events)
        self._escalation_mark += len(escalations)
        self._refill_mark += len(refills)
        self._usage_mark += len(usage)

    async def flush_session_end(self, session: SessionState) -> None:
        """Record that a session closed."""
        try:
            async with self.database.session() as db:
                await upsert_session(db, session)
        except Exception as exc:
            logger.error(
                "session_persistence_failed", session_id=session.session_id, error=str(exc)
            )
