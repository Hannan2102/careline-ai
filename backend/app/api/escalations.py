"""Escalations and refill requests -- the human work queue.

Both are read-only here. Marking an escalation handled or acting on a refill
is a *staff* action on a patient's record, and this API has no authentication
to hang that on; the endpoints arrive with staff auth, not before (SAFETY.md).
A refill request is never an authorisation in any case (FHIR.md).
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.db.repositories import list_escalations, list_refill_requests
from app.schemas.domain import EscalationCategory, Priority, RefillStatus

router = APIRouter(prefix="/api", tags=["dashboard"])


class EscalationView(BaseModel):
    model_config = ConfigDict(frozen=True)

    escalation_id: str
    category: str
    priority: str
    destination: str
    summary: str
    patient_ref: str | None
    verification_state: str
    medication_display: str | None
    patient_question: str | None
    ai_action: str
    session_id: str | None
    created_at: datetime


class RefillRequestView(BaseModel):
    model_config = ConfigDict(frozen=True)

    refill_request_id: str
    patient_ref: str
    medication_request_id: str
    medication_display: str
    status: str
    requested_at: datetime
    session_id: str | None


@router.get("/escalations", response_model=list[EscalationView])
async def escalations(
    category: EscalationCategory | None = None,
    priority: Priority | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> list[EscalationView]:
    rows = await list_escalations(
        db,
        category=category.value if category else None,
        priority=priority.value if priority else None,
        limit=limit,
    )
    return [
        EscalationView(
            escalation_id=row.escalation_id,
            category=row.category,
            priority=row.priority,
            destination=row.destination,
            summary=row.summary,
            patient_ref=row.patient_ref,
            verification_state=row.verification_state,
            medication_display=row.medication_display,
            patient_question=row.patient_question,
            ai_action=row.ai_action,
            session_id=row.session_id,
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.get("/medications/refill-requests", response_model=list[RefillRequestView])
async def refill_requests(
    status: RefillStatus | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> list[RefillRequestView]:
    rows = await list_refill_requests(db, status=status.value if status else None, limit=limit)
    return [
        RefillRequestView(
            refill_request_id=row.refill_request_id,
            patient_ref=row.patient_ref,
            medication_request_id=row.medication_request_id,
            medication_display=row.medication_display,
            status=row.status,
            requested_at=row.requested_at,
            session_id=row.session_id,
        )
        for row in rows
    ]
