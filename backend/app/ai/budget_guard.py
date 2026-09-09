"""Budget enforcement (ADR 006).

Consulted **before** any paid provider call. It lives here, consumed by the
provider factory rather than by individual call sites, so a future adapter
cannot forget to check it.

Blocking is not failing: when the ceiling is reached the factory substitutes a
mock provider and the system keeps working in text mode. A guard that breaks
the demo would simply get switched off, which would defeat the point.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from app.ai.usage import STT_SECONDS, TTS_CHARACTERS, UsageLedger
from app.config.settings import PAID_PROVIDERS, Settings

BudgetStatus = Literal["ok", "warn", "blocked"]


class BudgetDecision(BaseModel):
    """The outcome of a guard check, and why."""

    allowed: bool
    status: BudgetStatus
    project_total_usd: Decimal
    session_total_usd: Decimal
    project_limit_usd: Decimal
    remaining_usd: Decimal
    reason: str | None = None

    @property
    def is_free_provider(self) -> bool:
        return self.status == "ok" and self.reason == "provider is free"


class BudgetGuard:
    """Applies project and per-session ceilings to paid provider usage."""

    def __init__(self, settings: Settings, ledger: UsageLedger) -> None:
        self.settings = settings
        self.ledger = ledger

    def check(self, provider: str, session_id: str | None = None) -> BudgetDecision:
        """Decide whether ``provider`` may be called right now."""
        project_total = self.ledger.project_total()
        session_total = self.ledger.session_total(session_id) if session_id else Decimal("0")
        limit = self.settings.max_estimated_project_cost_usd
        remaining = limit - project_total

        def decide(allowed: bool, status: BudgetStatus, reason: str | None) -> BudgetDecision:
            return BudgetDecision(
                allowed=allowed,
                status=status,
                project_total_usd=project_total,
                session_total_usd=session_total,
                project_limit_usd=limit,
                remaining_usd=remaining,
                reason=reason,
            )

        # Free providers are never blocked: the fallback must always be reachable.
        if provider not in PAID_PROVIDERS:
            return decide(True, "ok", "provider is free")

        if self.settings.override_permitted:
            return decide(True, "warn", "BUDGET_GUARD_OVERRIDE is set (development only)")

        # Checks in order; the first that trips decides.
        if project_total >= limit:
            return decide(
                False,
                "blocked",
                f"estimated project spend ${project_total:.2f} has reached the "
                f"${limit:.2f} ceiling; falling back to mock providers",
            )

        if session_id:
            if session_total >= self.settings.max_estimated_session_cost_usd:
                return decide(
                    False,
                    "blocked",
                    f"session spend ${session_total:.2f} has reached the per-session cap of "
                    f"${self.settings.max_estimated_session_cost_usd:.2f}",
                )
            tts_chars = self.ledger.session_quantity(session_id, TTS_CHARACTERS)
            if tts_chars >= self.settings.max_tts_characters_per_session:
                return decide(False, "blocked", f"session TTS character cap reached ({tts_chars})")
            stt_seconds = self.ledger.session_quantity(session_id, STT_SECONDS)
            if stt_seconds >= self.settings.max_stt_minutes_per_session * 60:
                return decide(False, "blocked", f"session STT minute cap reached ({stt_seconds}s)")

        if project_total >= self.settings.warn_estimated_project_cost_usd:
            return decide(
                True,
                "warn",
                f"estimated project spend ${project_total:.2f} has passed the "
                f"${self.settings.warn_estimated_project_cost_usd:.2f} warning threshold; "
                f"${remaining:.2f} remaining",
            )

        return decide(True, "ok", None)

    def summary(self) -> dict[str, object]:
        """Budget state for ``/api/system/status`` and the dashboard."""
        decision = self.check("openai")
        return {
            "estimated_project_cost_usd": str(self.ledger.project_total()),
            "project_limit_usd": str(self.settings.max_estimated_project_cost_usd),
            "warn_threshold_usd": str(self.settings.warn_estimated_project_cost_usd),
            "remaining_usd": str(decision.remaining_usd),
            "status": decision.status,
            "paid_calls_allowed": decision.allowed,
            "override_active": self.settings.override_permitted,
            "by_provider": {k: str(v) for k, v in self.ledger.totals_by_provider().items()},
        }
