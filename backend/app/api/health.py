"""Liveness endpoint."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app import __version__

router = APIRouter(tags=["system"])


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Cheap liveness check.

    Deliberately touches no dependency, so it stays a true liveness signal.
    Dependency health belongs to ``/api/system/status``.
    """
    return HealthResponse(status="ok", service="careline-ai-backend", version=__version__)
