"""Shared FastAPI dependencies for the dashboard read APIs.

Kept in one place because the dashboard routers all need the same two things
and because a dependency with the wrong signature is expensive to debug here
(see the note in ``api/agent.py`` about parameter-free dependencies).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings, get_settings
from app.db.engine import Database
from app.ehr.base import EHRProvider
from app.ehr.factory import build_ehr_provider


def get_ehr(settings: Settings = Depends(get_settings)) -> EHRProvider:
    return build_ehr_provider(settings)


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """A read session, or a clear 503 when persistence is switched off.

    The dashboard reads what Phase 11 persists; with ``PERSISTENCE_ENABLED``
    false there is nothing to read, and saying so plainly beats returning
    empty lists that look like a quiet clinic.
    """
    database: Database | None = getattr(request.app.state, "database", None)
    if database is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Persistence is disabled, so there is no call history to read. "
                "Set PERSISTENCE_ENABLED=true and restart."
            ),
        )
    async with database.session() as session:
        yield session
