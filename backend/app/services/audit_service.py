"""Audit trail.

Append-only record of every access and mutation of patient data. It answers
"who looked at what, when, and what happened" -- the question any healthcare
system has to be able to answer, and one that is impossible to reconstruct
after the fact if it was not recorded at the time.

What it deliberately does **not** hold: demographics, dosage text, transcript
content. It records that a medication list was read, not what was in it.
Copying clinical content into a second store widens the blast radius without
improving the trail.

In-process for now; Phase 11 persists to the ``audit_event`` table.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from app.observability.logging import get_logger
from app.schemas.domain import AuditAction, AuditEvent

logger = get_logger(__name__)


class AuditStore:
    """Append-only in-process event log."""

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []

    def append(self, event: AuditEvent) -> None:
        self._events.append(event)

    def all(self) -> list[AuditEvent]:
        return list(self._events)

    def for_session(self, session_id: str) -> list[AuditEvent]:
        return [e for e in self._events if e.session_id == session_id]

    def for_patient(self, patient_ref: str) -> list[AuditEvent]:
        return [e for e in self._events if e.patient_ref == patient_ref]

    def actions(self) -> list[AuditAction]:
        return [e.action for e in self._events]

    def clear(self) -> None:
        self._events.clear()


class AuditService:
    """Records audit events."""

    def __init__(self, store: AuditStore | None = None) -> None:
        self.store = store if store is not None else AuditStore()

    def record(
        self,
        action: AuditAction,
        session_id: str | None = None,
        patient_ref: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        outcome: Literal["success", "denied", "failure"] = "success",
        detail: str | None = None,
        now: datetime | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            event_id=f"aud-{uuid.uuid4().hex[:12]}",
            action=action,
            session_id=session_id,
            patient_ref=patient_ref,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
            detail=detail,
            created_at=now or datetime.now(UTC),
        )
        self.store.append(event)
        logger.info(
            "audit_event",
            action=action.value,
            outcome=outcome,
            session_id=session_id,
            patient_ref=patient_ref,
            resource_id=resource_id,
        )
        return event
