"""Overview metrics and spend.

The overview page answers two questions: what did the agent do, and what did
it cost. Both are computed from persisted rows, so the numbers survive a
restart and match what ``make budget`` reports (COSTS.md).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.budget_guard import BudgetGuard
from app.ai.usage import get_usage_ledger
from app.api.deps import get_db
from app.config.settings import Settings, get_settings
from app.db.repositories import (
    cost_by_provider,
    cost_since,
    count_escalations_since,
    count_sessions_since,
    total_estimated_cost,
    turn_metrics,
)

router = APIRouter(prefix="/api", tags=["dashboard"])

#: The overview's "today" is a rolling 24 hours rather than a calendar day.
#: A demo run at 00:30 should still show its own calls.
WINDOW = timedelta(hours=24)


class UsageSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    estimated_cost_today_usd: str
    estimated_project_cost_usd: str
    project_limit_usd: str
    warn_threshold_usd: str
    remaining_usd: str
    status: str
    paid_calls_allowed: bool
    can_spend_money: bool
    by_provider: dict[str, str]


class Overview(BaseModel):
    model_config = ConfigDict(frozen=True)

    window_hours: int
    generated_at: datetime

    calls: int
    turns: int
    escalations: int
    average_turn_ms: float
    refused_turns: int
    turns_by_intent: dict[str, int]
    #: Keyed by audit action, so "appointment.booked" and "medication.read"
    #: are counted from what was actually written, not from intent guesses.
    actions: dict[str, int]

    usage: UsageSummary

    synthetic_data_only: bool = True


@router.get("/usage/summary", response_model=UsageSummary)
async def usage_summary(
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> UsageSummary:
    return await _usage_summary(db, settings)


async def _usage_summary(db: AsyncSession, settings: Settings) -> UsageSummary:
    guard = BudgetGuard(settings, get_usage_ledger())
    summary: dict[str, Any] = guard.summary()
    persisted = await total_estimated_cost(db)
    today = await cost_since(db, datetime.now(UTC) - WINDOW)
    by_provider = await cost_by_provider(db)
    return UsageSummary(
        estimated_cost_today_usd=str(today),
        # The persisted ledger is the authority: the in-process one only knows
        # about this boot, and the guard's baseline is restored from this.
        estimated_project_cost_usd=str(persisted),
        project_limit_usd=str(summary["project_limit_usd"]),
        warn_threshold_usd=str(summary["warn_threshold_usd"]),
        remaining_usd=str(summary["remaining_usd"]),
        status=str(summary["status"]),
        paid_calls_allowed=bool(summary["paid_calls_allowed"]),
        can_spend_money=settings.can_spend_money,
        by_provider={k: str(v) for k, v in by_provider.items()},
    )


@router.get("/overview", response_model=Overview)
async def overview(
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Overview:
    since = datetime.now(UTC) - WINDOW
    metrics = await turn_metrics(db, since=since)
    return Overview(
        window_hours=int(WINDOW.total_seconds() // 3600),
        generated_at=datetime.now(UTC),
        calls=await count_sessions_since(db, since),
        turns=metrics.turns,
        escalations=await count_escalations_since(db, since),
        average_turn_ms=round(metrics.average_total_ms, 1),
        refused_turns=metrics.refused_turns,
        turns_by_intent=metrics.turns_by_intent,
        actions=metrics.actions,
        usage=await _usage_summary(db, settings),
    )
