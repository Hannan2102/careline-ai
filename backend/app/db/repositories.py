"""Persistence for the application schema.

Plain functions over an ``AsyncSession`` rather than a repository class per
table: there is one writer (the persistence service) and a handful of readers
(the dashboard), and an abstraction layer between them would earn nothing yet.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
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

#: Intent recorded when nothing was recognised -- carried by confirmations and
#: by turns a workflow was already steering.
UNINFORMATIVE_INTENT = "unknown"


@dataclass
class SessionRollup:
    """Aggregates the calls list shows next to each session."""

    turns: int = 0
    estimated_cost: Decimal = Decimal("0")
    total_ms: float = 0.0
    last_turn_at: datetime | None = None
    escalations: int = 0
    last_intent: str | None = None
    workflow: str | None = None


@dataclass
class TurnMetrics:
    """Aggregates the overview page shows."""

    turns: int = 0
    average_total_ms: float = 0.0
    estimated_cost: Decimal = Decimal("0")
    refused_turns: int = 0
    turns_by_intent: dict[str, int] = field(default_factory=dict)
    actions: dict[str, int] = field(default_factory=dict)


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
        # Ending a session revokes live access by clearing the patient
        # reference (ADR 003). That is about *access*, not about history: the
        # row keeps the patient this call was actually verified against, or
        # the dashboard would show every finished call as "not identified".
        if key == "patient_ref" and value is None and existing.patient_ref is not None:
            continue
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


async def insert_audit_events(
    db: AsyncSession, events: list[AuditEvent], turn_id: str | None = None
) -> None:
    for event in events:
        db.add(
            AuditEventRow(
                event_id=event.event_id,
                action=event.action.value,
                session_id=event.session_id,
                turn_id=turn_id,
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


async def list_sessions(db: AsyncSession, limit: int = 50, offset: int = 0) -> list[SessionRow]:
    """Most recent conversations first."""
    result = await db.scalars(
        select(SessionRow).order_by(SessionRow.created_at.desc()).limit(limit).offset(offset)
    )
    return list(result)


async def get_session(db: AsyncSession, session_id: str) -> SessionRow | None:
    return await db.get(SessionRow, session_id)


async def turns_for_session(db: AsyncSession, session_id: str) -> list[TurnRow]:
    result = await db.scalars(
        select(TurnRow).where(TurnRow.session_id == session_id).order_by(TurnRow.turn_number)
    )
    return list(result)


async def audit_for_session(db: AsyncSession, session_id: str) -> list[AuditEventRow]:
    result = await db.scalars(
        select(AuditEventRow)
        .where(AuditEventRow.session_id == session_id)
        .order_by(AuditEventRow.created_at)
    )
    return list(result)


async def session_rollups(db: AsyncSession, session_ids: list[str]) -> dict[str, SessionRollup]:
    """Per-session aggregates for the calls list.

    One grouped query rather than a query per row: the calls page is a list,
    and a per-row query is the shape that quietly becomes N+1.
    """
    if not session_ids:
        return {}

    turn_rows = await db.execute(
        select(
            TurnRow.session_id,
            func.count(),
            func.sum(TurnRow.estimated_cost_usd),
            func.sum(TurnRow.total_ms),
            func.max(TurnRow.created_at),
        )
        .where(TurnRow.session_id.in_(session_ids))
        .group_by(TurnRow.session_id)
    )
    rollups = {
        session_id: SessionRollup(
            turns=int(turns or 0),
            estimated_cost=Decimal(str(cost or 0)),
            total_ms=float(total_ms or 0.0),
            last_turn_at=last_at,
        )
        for session_id, turns, cost, total_ms, last_at in turn_rows.all()
    }

    escalation_rows = await db.execute(
        select(EscalationRow.session_id, func.count())
        .where(EscalationRow.session_id.in_(session_ids))
        .group_by(EscalationRow.session_id)
    )
    for session_id, count in escalation_rows.all():
        if session_id in rollups:
            rollups[session_id].escalations = int(count or 0)

    intents = await db.execute(
        select(TurnRow.session_id, TurnRow.intent, TurnRow.workflow, TurnRow.turn_number)
        .where(TurnRow.session_id.in_(session_ids))
        .order_by(TurnRow.session_id, TurnRow.turn_number)
    )
    for session_id, intent, workflow, _number in intents.all():
        rollup = rollups.get(session_id)
        if rollup is None:
            continue
        # The *last* intent is usually "unknown": the final turn of a booking
        # is "Yes", which carries no intent of its own because the workflow
        # already has the turn. The last one that says something is what the
        # call was about.
        if intent != UNINFORMATIVE_INTENT or rollup.last_intent is None:
            rollup.last_intent = intent
        if workflow:
            rollup.workflow = workflow

    return rollups


async def list_escalations(
    db: AsyncSession,
    category: str | None = None,
    priority: str | None = None,
    limit: int = 100,
) -> list[EscalationRow]:
    statement = select(EscalationRow).order_by(EscalationRow.created_at.desc()).limit(limit)
    if category is not None:
        statement = statement.where(EscalationRow.category == category)
    if priority is not None:
        statement = statement.where(EscalationRow.priority == priority)
    result = await db.scalars(statement)
    return list(result)


async def list_refill_requests(
    db: AsyncSession, status: str | None = None, limit: int = 100
) -> list[RefillRequestRow]:
    statement = select(RefillRequestRow).order_by(RefillRequestRow.requested_at.desc()).limit(limit)
    if status is not None:
        statement = statement.where(RefillRequestRow.status == status)
    result = await db.scalars(statement)
    return list(result)


async def turn_metrics(db: AsyncSession, since: datetime | None = None) -> TurnMetrics:
    """Aggregates for the overview cards."""
    conditions = [TurnRow.created_at >= since] if since is not None else []

    row = (
        await db.execute(
            select(
                func.count(),
                func.avg(TurnRow.total_ms),
                func.sum(TurnRow.estimated_cost_usd),
            ).where(*conditions)
        )
    ).one()
    turns, average_ms, cost = row

    refused = await db.scalar(
        select(func.count())
        .select_from(TurnRow)
        .where(TurnRow.safety_outcome != "ALLOW", *conditions)
    )

    by_intent = await db.execute(
        select(TurnRow.intent, func.count()).where(*conditions).group_by(TurnRow.intent)
    )
    by_action = await db.execute(
        select(AuditEventRow.action, func.count())
        .where(*([AuditEventRow.created_at >= since] if since is not None else []))
        .group_by(AuditEventRow.action)
    )

    return TurnMetrics(
        turns=int(turns or 0),
        average_total_ms=float(average_ms or 0.0),
        estimated_cost=Decimal(str(cost or 0)),
        refused_turns=int(refused or 0),
        turns_by_intent={intent: int(count) for intent, count in by_intent.all()},
        actions={action: int(count) for action, count in by_action.all()},
    )


async def count_sessions_since(db: AsyncSession, since: datetime) -> int:
    result = await db.scalar(
        select(func.count()).select_from(SessionRow).where(SessionRow.created_at >= since)
    )
    return int(result or 0)


async def count_escalations_since(db: AsyncSession, since: datetime) -> int:
    result = await db.scalar(
        select(func.count()).select_from(EscalationRow).where(EscalationRow.created_at >= since)
    )
    return int(result or 0)


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
    "SessionRollup",
    "TurnMetrics",
    "audit_for_session",
    "cost_by_provider",
    "cost_since",
    "count_escalations_since",
    "count_rows",
    "count_sessions_since",
    "get_session",
    "insert_audit_events",
    "insert_escalations",
    "insert_turn",
    "insert_usage",
    "list_escalations",
    "list_refill_requests",
    "list_sessions",
    "session_rollups",
    "total_estimated_cost",
    "turn_metrics",
    "turns_for_session",
    "upsert_refill_requests",
    "upsert_session",
]
