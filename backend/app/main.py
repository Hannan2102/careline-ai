"""FastAPI application entrypoint.

Phase 1 exposes system endpoints only. Patient, appointment, medication,
escalation, and agent routers arrive with their phases (ROADMAP.md) -- they are
deliberately not stubbed with fake behaviour.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from app import __version__
from app.ai.usage import get_usage_ledger
from app.api import agent, health, system
from app.config.settings import EHRProviderName, get_settings
from app.db.engine import Database
from app.db.repositories import total_estimated_cost
from app.ehr.factory import get_default_memory_store
from app.ehr.seeding import seed_memory_store
from app.observability.logging import bind_trace, clear_trace, configure_logging, get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.app_env != "development")
    logger.info(
        "startup",
        version=__version__,
        environment=settings.app_env,
        ehr_provider=settings.ehr_provider.value,
        ai_mode=settings.ai_mode.value,
        text_only=settings.text_only_mode,
        can_spend_money=settings.can_spend_money,
    )

    # The in-memory store lives in this process, so it is seeded here rather
    # than by an external script (which would seed a store nobody can see).
    if settings.ehr_provider is EHRProviderName.MEMORY:
        summary = await seed_memory_store(get_default_memory_store())
        logger.info("seeded_memory_ehr", **summary)

    if settings.persistence_enabled:
        database = Database.from_settings(settings)
        await database.create_schema()
        app.state.database = database
        # Carry forward spend from previous runs before anything can spend
        # more, so the budget ceiling means "this project" and not "this boot".
        async with database.session() as db:
            carried = await total_estimated_cost(db)
        get_usage_ledger().set_baseline(carried)
        if carried > 0:
            logger.info("usage_baseline_restored", estimated_spend_usd=str(carried))

        # Install the runtime with persistence attached.
        from app.api.agent import configure_runtime

        configure_runtime(database=database)

    yield

    active_database: Database | None = getattr(app.state, "database", None)
    if active_database is not None:
        await active_database.dispose()
    logger.info("shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title="CareLine AI - Patient Access API",
        description=(
            "Agentic patient-access platform for the fictional Oakwood Family Medicine. "
            "**Synthetic data only. Not HIPAA compliant. Not for clinical use.**"
        ),
        version=__version__,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def trace_requests(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        trace_id = request.headers.get("X-Trace-Id") or uuid.uuid4().hex
        bind_trace(trace_id=trace_id, path=request.url.path)
        try:
            response = await call_next(request)
        finally:
            clear_trace()
        response.headers["X-Trace-Id"] = trace_id
        return response

    app.include_router(health.router)
    app.include_router(system.router)
    app.include_router(agent.router)
    return app


app = create_app()
