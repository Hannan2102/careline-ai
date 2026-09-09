"""Calls and agent traces -- the dashboard's read side.

Everything here comes from the persisted ``session``, ``turn``, and
``audit_event`` tables rather than the in-process stores, so a call is still
inspectable after the process that handled it has gone (Phase 11).

Read-only on purpose. Staff actions on a call would need authentication, and
there is none here (SAFETY.md).
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_ehr
from app.db.models import AuditEventRow, SessionRow, TurnRow
from app.db.repositories import (
    SessionRollup,
    audit_for_session,
    get_session,
    list_sessions,
    session_rollups,
    turns_for_session,
)
from app.ehr.base import EHRProvider

router = APIRouter(prefix="/api/calls", tags=["dashboard"])


class CallSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: str
    channel: str
    verification: str
    patient_ref: str | None
    patient_name: str | None
    started_at: datetime
    ended_at: datetime | None
    duration_seconds: float | None
    turns: int
    escalations: int
    last_intent: str | None
    workflow: str | None
    estimated_cost_usd: str
    outcome: str


class Operation(BaseModel):
    """One audit event: what the turn actually did to the record."""

    model_config = ConfigDict(frozen=True)

    event_id: str
    action: str
    outcome: str
    patient_ref: str | None
    resource_type: str | None
    resource_id: str | None
    detail: str | None
    created_at: datetime


class TurnDetail(BaseModel):
    """Every field of a turn record. The Agent Trace page renders all of it."""

    model_config = ConfigDict(frozen=True)

    turn_id: str
    turn_number: int
    created_at: datetime
    utterance: str
    response: str
    safety_outcome: str
    safety_category: str | None
    safety_rule: str | None
    intent: str
    confidence: float
    entities: dict[str, str]
    workflow: str | None
    workflow_state: str | None
    workflow_status: str | None
    escalation_id: str | None
    verification_state: str
    safety_ms: float
    extraction_ms: float
    workflow_ms: float
    total_ms: float
    stt_ms: float | None
    tts_first_audio_ms: float | None
    estimated_cost_usd: str
    operations: list[Operation]


class CallTrace(BaseModel):
    model_config = ConfigDict(frozen=True)

    call: CallSummary
    turns: list[TurnDetail]
    #: Audit events the flush could not attribute to a turn (older rows).
    unattributed_operations: list[Operation]


def outcome_of(row: SessionRow, rollup: SessionRollup) -> str:
    """A one-word answer to 'how did this call end?'.

    Derived rather than stored: the underlying facts (escalation count,
    verification state, whether the session was closed) are already recorded,
    and a second stored field would be one more thing to keep true.
    """
    if rollup.escalations:
        return "escalated"
    if row.verification == "FAILED":
        return "verification-failed"
    if row.ended_at is None:
        return "in-progress"
    return "resolved"


def _summary(row: SessionRow, rollup: SessionRollup, patient_name: str | None) -> CallSummary:
    last_moment = row.ended_at or rollup.last_turn_at
    duration = (last_moment - row.created_at).total_seconds() if last_moment else None
    return CallSummary(
        session_id=row.session_id,
        channel=row.channel,
        verification=row.verification,
        patient_ref=row.patient_ref,
        patient_name=patient_name,
        started_at=row.created_at,
        ended_at=row.ended_at,
        duration_seconds=duration,
        turns=rollup.turns or row.turn_count,
        escalations=rollup.escalations,
        last_intent=rollup.last_intent,
        workflow=rollup.workflow,
        estimated_cost_usd=str(rollup.estimated_cost),
        outcome=outcome_of(row, rollup),
    )


async def _name_index(ehr: EHRProvider, refs: set[str]) -> dict[str, str]:
    """Map patient references to names in one roster read, not one read each."""
    if not refs:
        return {}
    roster = await ehr.list_patients(limit=200)
    return {p.reference: p.full_name for p in roster if p.reference in refs}


def _operation(row: AuditEventRow) -> Operation:
    return Operation(
        event_id=row.event_id,
        action=row.action,
        outcome=row.outcome,
        patient_ref=row.patient_ref,
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        detail=row.detail,
        created_at=row.created_at,
    )


def _turn_detail(row: TurnRow, operations: list[Operation]) -> TurnDetail:
    return TurnDetail(
        turn_id=row.turn_id,
        turn_number=row.turn_number,
        created_at=row.created_at,
        utterance=row.utterance,
        response=row.response,
        safety_outcome=row.safety_outcome,
        safety_category=row.safety_category,
        safety_rule=row.safety_rule,
        intent=row.intent,
        confidence=row.confidence,
        entities=dict(row.entities or {}),
        workflow=row.workflow,
        workflow_state=row.workflow_state,
        workflow_status=row.workflow_status,
        escalation_id=row.escalation_id,
        verification_state=row.verification_state,
        safety_ms=row.safety_ms,
        extraction_ms=row.extraction_ms,
        workflow_ms=row.workflow_ms,
        total_ms=row.total_ms,
        stt_ms=row.stt_ms,
        tts_first_audio_ms=row.tts_first_audio_ms,
        estimated_cost_usd=str(row.estimated_cost_usd),
        operations=operations,
    )


@router.get("", response_model=list[CallSummary])
async def list_calls(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    ehr: EHRProvider = Depends(get_ehr),
) -> list[CallSummary]:
    rows = await list_sessions(db, limit=limit, offset=offset)
    rollups = await session_rollups(db, [row.session_id for row in rows])
    names = await _name_index(ehr, {row.patient_ref for row in rows if row.patient_ref})
    return [
        _summary(
            row,
            rollups.get(row.session_id, SessionRollup()),
            names.get(row.patient_ref or ""),
        )
        for row in rows
    ]


@router.get("/{session_id}/trace", response_model=CallTrace)
async def call_trace(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    ehr: EHRProvider = Depends(get_ehr),
) -> CallTrace:
    row = await get_session(db, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown session {session_id!r}")

    rollups = await session_rollups(db, [session_id])
    names = await _name_index(ehr, {row.patient_ref} if row.patient_ref else set())
    turns = await turns_for_session(db, session_id)
    events = await audit_for_session(db, session_id)

    by_turn: dict[str, list[Operation]] = {}
    unattributed: list[Operation] = []
    for event in events:
        if event.turn_id is None:
            unattributed.append(_operation(event))
        else:
            by_turn.setdefault(event.turn_id, []).append(_operation(event))

    return CallTrace(
        call=_summary(
            row, rollups.get(session_id, SessionRollup()), names.get(row.patient_ref or "")
        ),
        turns=[_turn_detail(turn, by_turn.get(turn.turn_id, [])) for turn in turns],
        unattributed_operations=unattributed,
    )
