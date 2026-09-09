"""Persistence of the application schema (Phase 11).

Uses in-memory SQLite, so these run offline and fast. The point of the phase is
that an audit trail which dies with the process is not an audit trail.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.agents.factory import Runtime, build_runtime
from app.ai.usage import OUTPUT_TOKENS, UsageLedger
from app.config.settings import Settings
from app.db.engine import Database
from app.db.models import (
    AuditEventRow,
    EscalationRow,
    ProviderUsageRow,
    RefillRequestRow,
    SessionRow,
    TurnRow,
)
from app.db.repositories import (
    cost_by_provider,
    count_rows,
    total_estimated_cost,
)
from app.ehr.base import EHRProvider
from app.schemas.domain import RefillStatus

IDENTIFY = "My name is John Smith and I was born 15 February 1985"


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.create_schema()
    yield db
    await db.dispose()


@pytest.fixture
def ledger() -> UsageLedger:
    return UsageLedger()


@pytest.fixture
def runtime(ehr: EHRProvider, database: Database, ledger: UsageLedger) -> Runtime:
    return build_runtime(
        ehr=ehr,
        settings=Settings(_env_file=None, app_env="test"),
        ledger=ledger,
        database=database,
    )


class TestSchema:
    async def test_the_schema_is_created(self, database: Database) -> None:
        async with database.session() as db:
            for model in (
                SessionRow,
                TurnRow,
                AuditEventRow,
                EscalationRow,
                RefillRequestRow,
                ProviderUsageRow,
            ):
                assert await count_rows(db, model) == 0

    async def test_an_in_memory_database_keeps_one_connection(self, database: Database) -> None:
        """Guards the StaticPool choice: without it the tables appear to vanish."""
        async with database.session() as first:
            assert await count_rows(first, SessionRow) == 0
        async with database.session() as second:
            assert await count_rows(second, SessionRow) == 0


class TestTurnPersistence:
    async def test_a_turn_is_written_with_its_session(
        self, runtime: Runtime, database: Database
    ) -> None:
        session = runtime.sessions.create()
        await runtime.orchestrator.handle_turn(session, "Are you open on Saturday?")

        async with database.session() as db:
            assert await count_rows(db, SessionRow) == 1
            assert await count_rows(db, TurnRow) == 1
            row = (await db.execute(select(TurnRow))).scalar_one()
            assert row.session_id == session.session_id
            assert row.intent == "clinic_faq"
            assert row.safety_outcome == "ALLOW"
            assert "closed on Saturday" in row.response

    async def test_the_session_row_tracks_verification(
        self, runtime: Runtime, database: Database
    ) -> None:
        session = runtime.sessions.create()
        await runtime.orchestrator.handle_turn(session, "When is my appointment?")
        await runtime.orchestrator.handle_turn(session, IDENTIFY)

        async with database.session() as db:
            row = (await db.execute(select(SessionRow))).scalar_one()
            assert row.verification == "VERIFIED"
            assert row.patient_ref == "Patient/demo-john-smith"
            assert row.turn_count == 2

    async def test_turns_accumulate_in_order(self, runtime: Runtime, database: Database) -> None:
        session = runtime.sessions.create()
        for utterance in ("Are you open on Saturday?", "Where are you located?"):
            await runtime.orchestrator.handle_turn(session, utterance)

        async with database.session() as db:
            rows = (await db.execute(select(TurnRow).order_by(TurnRow.turn_number))).scalars().all()
            assert [r.turn_number for r in rows] == [1, 2]

    async def test_latency_is_recorded_per_turn(self, runtime: Runtime, database: Database) -> None:
        session = runtime.sessions.create()
        await runtime.orchestrator.handle_turn(session, "Are you open on Saturday?")

        async with database.session() as db:
            row = (await db.execute(select(TurnRow))).scalar_one()
            assert row.total_ms > 0
            assert row.safety_ms >= 0

    async def test_persisted_turns_carry_no_identifiers(
        self, runtime: Runtime, database: Database
    ) -> None:
        """The utterance is the patient's own words; the entity map is not a copy of them."""
        session = runtime.sessions.create()
        await runtime.orchestrator.handle_turn(session, "When is my appointment?")
        await runtime.orchestrator.handle_turn(session, IDENTIFY)

        async with database.session() as db:
            rows = (await db.execute(select(TurnRow))).scalars().all()
            for row in rows:
                assert "John" not in str(row.entities)
                assert "1985" not in str(row.entities)


class TestAuditPersistence:
    async def test_audit_events_survive_the_turn(
        self, runtime: Runtime, database: Database
    ) -> None:
        session = runtime.sessions.create()
        await runtime.orchestrator.handle_turn(
            session, "I forgot how much Metformin I'm supposed to take"
        )
        await runtime.orchestrator.handle_turn(session, IDENTIFY)

        async with database.session() as db:
            actions = [
                row.action for row in (await db.execute(select(AuditEventRow))).scalars().all()
            ]
            assert "verification.succeeded" in actions
            assert "medication.read" in actions

    async def test_the_persisted_trail_holds_no_clinical_content(
        self, runtime: Runtime, database: Database
    ) -> None:
        session = runtime.sessions.create()
        await runtime.orchestrator.handle_turn(
            session, "I forgot how much Metformin I'm supposed to take"
        )
        await runtime.orchestrator.handle_turn(session, IDENTIFY)

        async with database.session() as db:
            rows = (await db.execute(select(AuditEventRow))).scalars().all()
            serialised = " ".join(f"{r.detail} {r.resource_id} {r.patient_ref}" for r in rows)
            assert "twice daily" not in serialised
            assert "Smith" not in serialised

    async def test_each_event_is_written_once(self, runtime: Runtime, database: Database) -> None:
        """The high-water mark must not re-write earlier turns' events."""
        session = runtime.sessions.create()
        for _ in range(3):
            await runtime.orchestrator.handle_turn(session, "Are you open on Saturday?")

        async with database.session() as db:
            rows = (await db.execute(select(AuditEventRow))).scalars().all()
            assert len({r.event_id for r in rows}) == len(rows)


class TestEscalationPersistence:
    async def test_a_clinical_escalation_is_stored_in_full(
        self, runtime: Runtime, database: Database
    ) -> None:
        session = runtime.sessions.create()
        utterance = "My Lisinopril makes me dizzy. Should I take half?"
        await runtime.orchestrator.handle_turn(session, utterance)

        async with database.session() as db:
            row = (await db.execute(select(EscalationRow))).scalar_one()
            assert row.category == "clinical"
            assert row.priority == "clinical"
            assert row.destination == "Nurse / clinical staff"
            assert row.ai_action == "No dosage recommendation provided"
            # The clinician needs the patient's own words.
            assert row.patient_question == utterance


class TestRefillPersistence:
    async def test_a_refill_request_is_stored_pending_review(
        self, runtime: Runtime, database: Database
    ) -> None:
        session = runtime.sessions.create()
        await runtime.orchestrator.handle_turn(session, "I need a refill on my metformin")
        await runtime.orchestrator.handle_turn(session, IDENTIFY)
        await runtime.orchestrator.handle_turn(session, "yes please")

        async with database.session() as db:
            row = (await db.execute(select(RefillRequestRow))).scalar_one()
            assert row.status == RefillStatus.PENDING_REVIEW.value
            assert row.medication_request_id == "medreq-john-metformin"
            assert row.session_id == session.session_id


class TestUsagePersistence:
    async def test_metered_usage_is_written(
        self, runtime: Runtime, database: Database, ledger: UsageLedger
    ) -> None:
        session = runtime.sessions.create()
        ledger.record("openai", OUTPUT_TOKENS, 1000, session_id=session.session_id)
        await runtime.orchestrator.handle_turn(session, "Are you open on Saturday?")

        async with database.session() as db:
            assert await count_rows(db, ProviderUsageRow) >= 1
            assert await total_estimated_cost(db) > Decimal("0")
            by_provider = await cost_by_provider(db)
            assert "openai" in by_provider

    async def test_the_persisted_ledger_is_what_the_budget_reads(
        self, runtime: Runtime, database: Database, ledger: UsageLedger
    ) -> None:
        """Project spend must survive a restart, or the ceiling means nothing."""
        session = runtime.sessions.create()
        ledger.record("openai", OUTPUT_TOKENS, 5_000_000, session_id=session.session_id)
        await runtime.orchestrator.handle_turn(session, "Are you open on Saturday?")

        async with database.session() as db:
            persisted = await total_estimated_cost(db)
        assert persisted == ledger.project_total()


class TestFailureHandling:
    async def test_a_persistence_failure_does_not_break_the_conversation(
        self, runtime: Runtime, database: Database
    ) -> None:
        """The caller is on the phone; a database problem must not end the call."""
        await database.dispose()  # every later write will fail

        session = runtime.sessions.create()
        result = await runtime.orchestrator.handle_turn(session, "Are you open on Saturday?")

        assert "closed on Saturday and Sunday" in result.message
        assert runtime.traces.for_session(session.session_id)
