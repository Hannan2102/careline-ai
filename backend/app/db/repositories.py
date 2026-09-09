"""Persistence for the application schema.

Plain functions over an ``AsyncSession`` rather than a repository class per
table: there is one writer (the persistence service) and a handful of readers
(the dashboard), and an abstraction layer between them would earn nothing yet.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.state import SessionState
from app.agents.trace import TurnTrace
from app.db.models import (
    AuditEventRow,
    EscalationRow,
    ProviderUsageRow,
    RefillRequestRow,
    SessionRow,
    TurnRow,
)
from app.schemas.domain import AuditEvent, Escalation, ProviderUsageRecord, RefillRequest


async def upsert_session(db: AsyncSession, session: SessionState) -> None:
    """Write the session's current state, creating the row if needed."""
    snapshot = session.snapshot()
    values: dict[str, Any] = {
        "session_id": snapshot.session_id,
        "channel": snapshot.channel.value,
        "verification": snapshot.verification.value,
        "patient_ref": snapshot.patient_ref,
        "failed_attempts": snapshot.failed_attempts,
        "turn_count": snapshot.turn_count,
        "created_at": snapshot.created_at,
        "ended_at": snapshot.ended_at,
    }
    existing = await db.get(SessionRow, snapshot.session_id)
    if existing is None:
        db.add(SessionRow(**values))
        return
    for key, value in values.items():
        setattr(existing, key, value)


async def insert_turn(db: AsyncSession, trace: TurnTrace) -> None:
    db.add(
        TurnRow(
            turn_id=trace.turn_id,
            session_id=trace.session_id,
            turn_number=trace.turn_number,
            created_at=trace.created_at,
            utterance=trace.utterance,
            response=trace.response,
            safety_outcome=trace.safety_outcome.value,
            safety_category=trace.safety_category.value if trace.safety_category else None,
            safety_rule=trace.safety_rule,
            intent=trace.intent.value,
            confidence=trace.confidence,
            entities=dict(trace.entities),
            workflow=trace.workflow,
            workflow_state=trace.workflow_state,
            workflow_status=trace.workflow_status,
            escalation_id=trace.escalation_id,
            verification_state=trace.verification_state,
            safety_ms=trace.timings.safety_ms,
            extraction_ms=trace.timings.extraction_ms,
            workflow_ms=trace.timings.workflow_ms,
            total_ms=trace.timings.total_ms,
            stt_ms=trace.timings.stt_ms,
            tts_first_audio_ms=trace.timings.tts_first_audio_ms,
            estimated_cost_usd=Decimal(trace.estimated_cost_usd),
        )
    )


async def insert_audit_events(db: AsyncSession, events: list[AuditEvent]) -> None:
    for event in events:
        db.add(
            AuditEventRow(
                event_id=event.event_id,
                action=event.action.value,
                session_id=event.session_id,
                patient_ref=event.patient_ref,
                resource_type=event.resource_type,
                resource_id=event.resource_id,
                outcome=event.outcome,
                detail=event.detail,
                created_at=event.created_at,
            )
        )


async def insert_escalations(db: AsyncSession, escalations: list[Escalation]) -> None:
    for escalation in escalations:
        db.add(
            EscalationRow(
                escalation_id=escalation.escalation_id,
                category=escalation.category.value,
                priority=escalation.priority.value,
                destination=escalation.destination,
                summary=escalation.summary,
                patient_ref=escalation.patient_ref,
                verification_state=escalation.verification_state.value,
                medication_display=escalation.medication_display,
                patient_question=escalation.patient_question,
                ai_action=escalation.ai_action,
                session_id=escalation.session_id,
                created_at=escalation.created_at,
            )
        )


async def upsert_refill_requests(db: AsyncSession, requests: list[RefillRequest]) -> None:
    for request in requests:
        existing = await db.get(RefillRequestRow, request.refill_request_id)
        if existing is not None:
            existing.status = request.status.value
            continue
        db.add(
            RefillRequestRow(
                refill_request_id=request.refill_request_id,
                patient_ref=request.patient_ref,
                medication_request_id=request.medication_request_id,
                medication_display=request.medication_display,
                status=request.status.value,
                requested_at=request.requested_at,
                session_id=request.session_id,
            )
        )


async def insert_usage(db: AsyncSession, records: list[ProviderUsageRecord]) -> None:
    for record in records:
        db.add(
            ProviderUsageRow(
                id=f"use-{uuid.uuid4().hex[:12]}",
                provider=record.provider,
                session_id=record.session_id,
                metric=record.metric,
                quantity=record.quantity,
                estimated_cost=record.estimated_cost,
                created_at=record.recorded_at,
            )
        )


# --------------------------------------------------------------------------
# Reads -- what the dashboard needs (Phase 10)
# --------------------------------------------------------------------------


async def total_estimated_cost(db: AsyncSession) -> Decimal:
    """Project-to-date spend, from the persisted ledger."""
    result = await db.scalar(select(func.sum(ProviderUsageRow.estimated_cost)))
    return Decimal(str(result)) if result is not None else Decimal("0")


async def cost_since(db: AsyncSession, moment: datetime) -> Decimal:
    result = await db.scalar(
        select(func.sum(ProviderUsageRow.estimated_cost)).where(
            ProviderUsageRow.created_at >= moment
        )
    )
    return Decimal(str(result)) if result is not None else Decimal("0")


async def cost_by_provider(db: AsyncSession) -> dict[str, Decimal]:
    rows = await db.execute(
        select(ProviderUsageRow.provider, func.sum(ProviderUsageRow.estimated_cost)).group_by(
            ProviderUsageRow.provider
        )
    )
    return {provider: Decimal(str(total)) for provider, total in rows.all()}


async def count_rows(db: AsyncSession, model: type[Any]) -> int:
    result = await db.scalar(select(func.count()).select_from(model))
    return int(result or 0)


__all__ = [
    "cost_by_provider",
    "cost_since",
    "count_rows",
    "insert_audit_events",
    "insert_escalations",
    "insert_turn",
    "insert_usage",
    "total_estimated_cost",
    "upsert_refill_requests",
    "upsert_session",
]
