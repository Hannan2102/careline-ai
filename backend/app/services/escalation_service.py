"""Human handoff.

An escalation preserves enough context that a member of staff can pick the
conversation up without making the patient start again (SAFETY.md). Phase 8
adds the clinical safety policies that create most of them; Phase 3 needs the
failed-verification case, so the record and the service exist now.

Storage is in-process. Phase 11 persists these to the ``escalation`` table;
the interface here is what callers depend on, so persistence is a swap behind
it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.observability.logging import get_logger
from app.schemas.domain import Escalation, EscalationCategory, Priority, VerificationState

logger = get_logger(__name__)

#: Where each category is routed. Fixed per category so a refusal cannot be
#: talked into a different destination.
DESTINATIONS: dict[EscalationCategory, str] = {
    EscalationCategory.CLINICAL: "Nurse / clinical staff",
    EscalationCategory.FAILED_VERIFICATION: "Front desk",
    EscalationCategory.PATIENT_REQUESTED: "Front desk",
    EscalationCategory.SYSTEM_UNCERTAINTY: "Front desk",
    EscalationCategory.ADMINISTRATIVE: "Front desk",
}

DEFAULT_PRIORITIES: dict[EscalationCategory, Priority] = {
    EscalationCategory.CLINICAL: Priority.CLINICAL,
    EscalationCategory.FAILED_VERIFICATION: Priority.ADMINISTRATIVE,
    EscalationCategory.PATIENT_REQUESTED: Priority.PATIENT_REQUESTED,
    EscalationCategory.SYSTEM_UNCERTAINTY: Priority.SYSTEM_UNCERTAINTY,
    EscalationCategory.ADMINISTRATIVE: Priority.ADMINISTRATIVE,
}


class EscalationStore:
    """In-process escalation records."""

    def __init__(self) -> None:
        self._records: list[Escalation] = []

    def add(self, escalation: Escalation) -> None:
        self._records.append(escalation)

    def all(self) -> list[Escalation]:
        return list(self._records)

    def for_session(self, session_id: str) -> list[Escalation]:
        return [e for e in self._records if e.session_id == session_id]

    def clear(self) -> None:
        self._records.clear()


class EscalationService:
    """Creates structured handoffs."""

    def __init__(self, store: EscalationStore | None = None) -> None:
        self.store = store if store is not None else EscalationStore()

    def create(
        self,
        category: EscalationCategory,
        summary: str,
        session_id: str | None = None,
        patient_ref: str | None = None,
        verification_state: VerificationState = VerificationState.UNVERIFIED,
        patient_question: str | None = None,
        medication_display: str | None = None,
        ai_action: str = "No clinical advice provided",
        priority: Priority | None = None,
        now: datetime | None = None,
    ) -> Escalation:
        """Record a handoff and return it.

        Destination and priority are derived from the category rather than
        supplied, so they cannot be influenced by conversation content.
        """
        escalation = Escalation(
            escalation_id=f"esc-{uuid.uuid4().hex[:12]}",
            category=category,
            priority=priority or DEFAULT_PRIORITIES[category],
            destination=DESTINATIONS[category],
            summary=summary,
            patient_ref=patient_ref,
            verification_state=verification_state,
            medication_display=medication_display,
            patient_question=patient_question,
            ai_action=ai_action,
            created_at=now or datetime.now(UTC),
            session_id=session_id,
        )
        self.store.add(escalation)
        logger.warning(
            "escalation_created",
            escalation_id=escalation.escalation_id,
            category=category.value,
            priority=escalation.priority.value,
            destination=escalation.destination,
            session_id=session_id,
            patient_ref=patient_ref,
        )
        return escalation
