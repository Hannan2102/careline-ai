"""System status: configuration and dependency health."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app import __version__
from app.ai.budget_guard import BudgetGuard
from app.ai.usage import get_usage_ledger
from app.config.settings import Settings, get_settings
from app.ehr.base import EHRProvider
from app.ehr.factory import build_ehr_provider

router = APIRouter(prefix="/api/system", tags=["system"])


class SystemStatus(BaseModel):
    status: str
    version: str
    environment: str
    synthetic_data_only: bool
    ehr: dict[str, Any]
    ai: dict[str, Any]
    modes: dict[str, bool]
    budget: dict[str, Any]


def get_ehr(settings: Settings = Depends(get_settings)) -> EHRProvider:
    return build_ehr_provider(settings)


@router.get("/status", response_model=SystemStatus)
async def system_status(
    settings: Settings = Depends(get_settings),
    ehr: EHRProvider = Depends(get_ehr),
) -> SystemStatus:
    """Diagnostic view of how this instance is configured and what it can reach.

    Reports ``degraded`` when the EHR is unreachable rather than failing: the
    dashboard needs to *show* that state, not lose the endpoint that reports it.
    """
    ehr_reachable = await ehr.ping()
    guard = BudgetGuard(settings, get_usage_ledger())

    return SystemStatus(
        status="ok" if ehr_reachable else "degraded",
        version=__version__,
        environment=settings.app_env,
        synthetic_data_only=True,
        ehr={
            "provider": ehr.name,
            "reachable": ehr_reachable,
            "fhir_base_url": settings.fhir_base_url
            if settings.ehr_provider.value == "local"
            else None,
        },
        ai={
            "mode": settings.ai_mode.value,
            "llm_provider": settings.llm_provider,
            "stt_provider": settings.stt_provider,
            "tts_provider": settings.tts_provider,
            "paid_providers_selected": sorted(settings.selected_paid_providers),
            "can_spend_money": settings.can_spend_money,
        },
        modes={
            "text_only": settings.text_only_mode,
            "voice_enabled": settings.voice_enabled,
            "stt_enabled": settings.stt_enabled,
            "tts_enabled": settings.tts_enabled,
        },
        budget=guard.summary(),
    )
