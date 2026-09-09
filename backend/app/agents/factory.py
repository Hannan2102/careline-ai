"""Assembling the runtime.

One place that wires services, workflows, and the orchestrator together, so the
API, the CLI, and the tests all exercise the same object graph. If they drifted,
"it works in the CLI" would stop meaning anything.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.agents.orchestrator import Orchestrator
from app.agents.trace import TraceStore
from app.ai.usage import UsageLedger, get_usage_ledger
from app.config.settings import Settings, get_settings
from app.db.engine import Database
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
