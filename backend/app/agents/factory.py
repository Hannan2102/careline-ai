"""Assembling the runtime.

One place that wires services, workflows, and the orchestrator together, so the
API, the CLI, and the tests all exercise the same object graph. If they drifted,
"it works in the CLI" would stop meaning anything.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.agents.orchestrator import Orchestrator
from app.agents.state import SessionState
from app.agents.trace import TraceStore
from app.ai.usage import UsageLedger, get_usage_ledger
from app.config.settings import Settings, get_settings
from app.db.engine import Database
from app.db.repositories import total_estimated_cost
from app.ehr.base import EHRProvider
from app.ehr.factory import build_ehr_provider
from app.services.audit_service import AuditService
from app.services.escalation_service import EscalationService
from app.services.medication_service import MedicationService
from app.services.patient_service import PatientService
from app.services.persistence_service import PersistenceService
from app.services.refill_service import RefillService
from app.services.safety_service import SafetyService
from app.services.scheduling_service import SchedulingService
from app.services.session_service import SessionStore
from app.services.verification_service import VerificationService


@dataclass
class Runtime:
    """The assembled agent runtime and the stores the dashboard reads."""

    orchestrator: Orchestrator
    sessions: SessionStore
    traces: TraceStore
    audit: AuditService
    escalations: EscalationService
    refills: RefillService
    ehr: EHRProvider
    ledger: UsageLedger
    database: Database | None = None
    persistence: PersistenceService | None = None

    async def end_session(self, session_id: str) -> SessionState:
        """End a session and record that it closed.

        Ending is two things -- the state change and the write -- and every
        caller wants both. Leaving the write to the caller is how the API and
        the CLI end up disagreeing about whether a call ever finished.
        """
        session = self.sessions.end(session_id)
        if self.persistence is not None:
            await self.persistence.flush_session_end(session)
        return session


async def open_database(
    settings: Settings | None = None, ledger: UsageLedger | None = None
) -> Database | None:
    """Open the application database and restore the spend baseline.

    Returns ``None`` when persistence is switched off. Shared by the API's
    lifespan and the CLI so that a conversation is recorded identically
    whichever one is driving -- a CLI that persisted nothing would mean
    `make chat` produced calls the dashboard could never show.
    """
    resolved = settings or get_settings()
    if not resolved.persistence_enabled:
        return None

    database = Database.from_settings(resolved)
    await database.create_schema()
    # Carry forward previous spend before anything can spend more, so the
    # budget ceiling means "this project" and not "this boot" (COSTS.md).
    async with database.session() as db:
        carried = await total_estimated_cost(db)
    (ledger or get_usage_ledger()).set_baseline(carried)
    return database


def build_runtime(
    ehr: EHRProvider | None = None,
    settings: Settings | None = None,
    ledger: UsageLedger | None = None,
    database: Database | None = None,
) -> Runtime:
    """Construct a complete runtime.

    Pass ``database`` to persist turns; without one the runtime keeps its
    working set in memory, which is what the offline test suite uses.
    """
    resolved_settings = settings or get_settings()
    provider = ehr or build_ehr_provider(resolved_settings)
    usage = ledger or get_usage_ledger()

    escalations = EscalationService()
    audit = AuditService()
    traces = TraceStore()
    refills = RefillService()

    persistence = (
        PersistenceService(database, audit, escalations, refills, usage)
        if database is not None
        else None
    )

    patients = PatientService(provider)
    orchestrator = Orchestrator(
        safety=SafetyService(escalations=escalations),
        verification=VerificationService(patients, escalations),
        scheduling=SchedulingService(provider),
        medications=MedicationService(provider),
        refills=refills,
        escalations=escalations,
        audit=audit,
        traces=traces,
        ledger=usage,
        settings=resolved_settings,
        persistence=persistence,
    )
    return Runtime(
        orchestrator=orchestrator,
        sessions=SessionStore(),
        traces=traces,
        audit=audit,
        escalations=escalations,
        refills=refills,
        ehr=provider,
        ledger=usage,
        database=database,
        persistence=persistence,
    )
