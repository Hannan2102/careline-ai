"""Refill requests.

A refill request is a **workflow artifact awaiting clinician review**, not a
clinical order. It is stored in the application schema and never written to the
EHR as a MedicationRequest, because a MedicationRequest is a prescription and
representing a patient's request as one would be exactly the confusion
SAFETY.md forbids (see FHIR.md for the reasoning).

There is deliberately no method here that approves, denies, or fulfils a
request. Every record is created in ``PENDING_REVIEW`` and only a clinician,
working outside this system, moves it on. The system cannot authorise a refill
because there is no code path that does.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.observability.logging import get_logger
from app.schemas.domain import MedicationSummary, RefillRequest, RefillStatus
from app.services.base import ConflictError, ValidationError

logger = get_logger(__name__)


class RefillStore:
    """In-process refill requests. Phase 11 persists to ``refill_request``."""

    def __init__(self) -> None:
        self._requests: list[RefillRequest] = []

    def add(self, request: RefillRequest) -> None:
        self._requests.append(request)

    def all(self) -> list[RefillRequest]:
        return list(self._requests)

    def for_patient(self, patient_ref: str) -> list[RefillRequest]:
        return [r for r in self._requests if r.patient_ref == patient_ref]

    def pending(self) -> list[RefillRequest]:
        return [r for r in self._requests if r.status is RefillStatus.PENDING_REVIEW]

    def clear(self) -> None:
        self._requests.clear()


class RefillService:
    """Creates refill requests for clinician review."""

    def __init__(self, store: RefillStore | None = None) -> None:
        self.store = store if store is not None else RefillStore()

    def request_refill(
        self,
        patient_ref: str,
        medication: MedicationSummary,
        session_id: str | None = None,
        now: datetime | None = None,
    ) -> RefillRequest:
        """Record a request against an active prescription.

        Raises:
            ValidationError: the prescription is not active, or belongs to
                someone else. A refill only makes sense against a live order.
            ConflictError: a request for this medication is already awaiting
                review -- sending a second would not make it happen sooner and
                would clutter the clinician's queue.
        """
        if medication.patient_ref != patient_ref:
            # Defence in depth: the gate should already have caught this.
            raise ValidationError("medication does not belong to this patient")

        if medication.status != "active":
            raise ValidationError(
                f"prescription {medication.medication_request_id!r} is "
                f"{medication.status!r}, not active"
            )

        duplicate = next(
            (
                existing
                for existing in self.store.for_patient(patient_ref)
                if existing.medication_request_id == medication.medication_request_id
                and existing.status is RefillStatus.PENDING_REVIEW
            ),
            None,
        )
        if duplicate is not None:
            raise ConflictError(
                f"a refill request for {medication.display_name} is already awaiting review"
            )

        request = RefillRequest(
            refill_request_id=f"rfl-{uuid.uuid4().hex[:12]}",
            patient_ref=patient_ref,
            medication_request_id=medication.medication_request_id,
            medication_display=medication.display_name,
            # Always pending. There is no argument that changes this.
            status=RefillStatus.PENDING_REVIEW,
            requested_at=now or datetime.now(UTC),
            session_id=session_id,
        )
        self.store.add(request)
        logger.info(
            "refill_requested",
            refill_request_id=request.refill_request_id,
            patient_ref=patient_ref,
            medication_request_id=medication.medication_request_id,
            status=request.status.value,
        )
        return request

    def pending_for_patient(self, patient_ref: str) -> list[RefillRequest]:
        return [
            r
            for r in self.store.for_patient(patient_ref)
            if r.status is RefillStatus.PENDING_REVIEW
        ]
