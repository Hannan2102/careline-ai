"""The dashboard's read APIs (Phase 10).

Data is produced the real way -- by running turns through the orchestrator,
which persists them -- and then read back over HTTP. Nothing here fabricates
rows directly, so a change that stops a turn being recorded correctly fails
these tests rather than passing them quietly.

Offline: in-memory SQLite, the in-memory EHR, mock providers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import httpx
import pytest
from tests.conftest import SEED_TODAY

from app.agents.factory import Runtime, build_runtime
from app.api.deps import get_ehr
from app.config.settings import Settings
from app.db.engine import Database
from app.ehr.base import EHRProvider
from app.main import create_app

IDENTIFY = "My name is John Smith and I was born 15 February 1985"
WINDOW = {
    "start_date": SEED_TODAY.isoformat(),
    "end_date": (SEED_TODAY + timedelta(days=30)).isoformat(),
}


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.create_schema()
    yield db
    await db.dispose()


@pytest.fixture
def runtime(memory_ehr: EHRProvider, database: Database) -> Runtime:
    return build_runtime(
        ehr=memory_ehr,
        settings=Settings(_env_file=None, app_env="test"),
        database=database,
    )


@pytest.fixture
async def client(memory_ehr: EHRProvider, database: Database) -> AsyncIterator[httpx.AsyncClient]:
    """The app without its lifespan, wired to the test database and EHR.

    Skipping the lifespan is deliberate: it would open the developer's own
    SQLite file and seed a second EHR store, and the test would then be reading
    something other than what it wrote.
    """
    app = create_app()
    app.state.database = database
    app.dependency_overrides[get_ehr] = lambda: memory_ehr
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


async def a_completed_call(runtime: Runtime) -> str:
    """A verified booking-lookup call, persisted. Returns its session id."""
    session = runtime.sessions.create()
    await runtime.orchestrator.handle_turn(session, "When is my appointment?")
    await runtime.orchestrator.handle_turn(session, IDENTIFY)
    return session.session_id


class TestCalls:
    async def test_a_call_appears_with_its_rollups(
        self, runtime: Runtime, client: httpx.AsyncClient
    ) -> None:
        session_id = await a_completed_call(runtime)

        body = (await client.get("/api/calls")).json()
        call = next(c for c in body if c["session_id"] == session_id)
        assert call["turns"] == 2
        assert call["verification"] == "VERIFIED"
        assert call["patient_name"] == "John Smith"
        assert call["outcome"] == "in-progress"
        assert call["duration_seconds"] >= 0

    async def test_an_ended_call_reports_a_resolved_outcome(
        self, runtime: Runtime, client: httpx.AsyncClient
    ) -> None:
        session_id = await a_completed_call(runtime)
        await runtime.end_session(session_id)

        body = (await client.get("/api/calls")).json()
        assert next(c for c in body if c["session_id"] == session_id)["outcome"] == "resolved"

    async def test_an_ended_call_keeps_the_patient_it_was_about(
        self, runtime: Runtime, client: httpx.AsyncClient
    ) -> None:
        """Ending revokes access, not history.

        `SessionState.end` clears the patient reference so a closed session
        cannot be used to reach the record (ADR 003). Persisting that cleared
        snapshot over the row would erase which patient the call concerned,
        and every finished call would read "not identified".
        """
        session_id = await a_completed_call(runtime)
        await runtime.end_session(session_id)

        call = next(
            c for c in (await client.get("/api/calls")).json() if c["session_id"] == session_id
        )
        assert call["patient_ref"] == "Patient/demo-john-smith"
        assert call["patient_name"] == "John Smith"
        assert call["ended_at"] is not None

    async def test_the_last_meaningful_intent_is_shown(
        self, runtime: Runtime, client: httpx.AsyncClient
    ) -> None:
        """A trailing "yes" carries no intent; the call is not about nothing."""
        session = runtime.sessions.create()
        await runtime.orchestrator.handle_turn(session, "When is my appointment?")
        await runtime.orchestrator.handle_turn(session, IDENTIFY)
        await runtime.orchestrator.handle_turn(session, "yes")

        call = next(
            c
            for c in (await client.get("/api/calls")).json()
            if c["session_id"] == session.session_id
        )
        assert call["last_intent"] != "unknown"

    async def test_an_escalated_call_says_so(
        self, runtime: Runtime, client: httpx.AsyncClient
    ) -> None:
        session = runtime.sessions.create()
        await runtime.orchestrator.handle_turn(session, "Can I double the dose of my lisinopril?")

        body = (await client.get("/api/calls")).json()
        call = next(c for c in body if c["session_id"] == session.session_id)
        assert call["escalations"] >= 1
        assert call["outcome"] == "escalated"

    async def test_calls_are_newest_first(
        self, runtime: Runtime, client: httpx.AsyncClient
    ) -> None:
        for _ in range(3):
            await a_completed_call(runtime)
        started = [c["started_at"] for c in (await client.get("/api/calls")).json()]
        assert started == sorted(started, reverse=True)

    async def test_the_list_is_empty_before_any_call(self, client: httpx.AsyncClient) -> None:
        assert (await client.get("/api/calls")).json() == []


class TestAgentTrace:
    async def test_every_turn_field_is_present(
        self, runtime: Runtime, client: httpx.AsyncClient
    ) -> None:
        session_id = await a_completed_call(runtime)

        trace = (await client.get(f"/api/calls/{session_id}/trace")).json()
        assert [t["turn_number"] for t in trace["turns"]] == [1, 2]

        turn = trace["turns"][0]
        # The acceptance criterion for this phase: the trace renders every
        # field of a turn record, latency and cost included.
        for field in (
            "turn_id",
            "created_at",
            "utterance",
            "response",
            "safety_outcome",
            "safety_category",
            "safety_rule",
            "intent",
            "confidence",
            "entities",
            "workflow",
            "workflow_state",
            "workflow_status",
            "escalation_id",
            "verification_state",
            "safety_ms",
            "extraction_ms",
            "workflow_ms",
            "total_ms",
            "stt_ms",
            "tts_first_audio_ms",
            "estimated_cost_usd",
            "operations",
        ):
            assert field in turn, field
        assert turn["total_ms"] > 0

    async def test_operations_are_attributed_to_their_turn(
        self, runtime: Runtime, client: httpx.AsyncClient
    ) -> None:
        session_id = await a_completed_call(runtime)

        trace = (await client.get(f"/api/calls/{session_id}/trace")).json()
        assert trace["unattributed_operations"] == []
        verifying_turn = trace["turns"][1]
        actions = [op["action"] for op in verifying_turn["operations"]]
        assert "verification.succeeded" in actions
        assert "appointment.read" in actions

    async def test_a_refusal_records_its_rule(
        self, runtime: Runtime, client: httpx.AsyncClient
    ) -> None:
        session = runtime.sessions.create()
        await runtime.orchestrator.handle_turn(session, "Can I double the dose of my lisinopril?")

        trace = (await client.get(f"/api/calls/{session.session_id}/trace")).json()
        turn = trace["turns"][0]
        assert turn["safety_outcome"] != "ALLOW"
        assert turn["safety_rule"]
        assert turn["escalation_id"]

    async def test_an_unknown_session_is_a_404(self, client: httpx.AsyncClient) -> None:
        assert (await client.get("/api/calls/nope/trace")).status_code == 404


class TestEscalationsAndRefills:
    async def test_an_escalation_carries_its_handoff(
        self, runtime: Runtime, client: httpx.AsyncClient
    ) -> None:
        session = runtime.sessions.create()
        await runtime.orchestrator.handle_turn(session, "Can I double the dose of my lisinopril?")

        body = (await client.get("/api/escalations")).json()
        assert body
        escalation = body[0]
        assert escalation["category"] == "clinical"
        assert escalation["patient_question"]
        assert escalation["ai_action"]
        assert escalation["summary"]

    async def test_escalations_can_be_filtered_by_category(
        self, runtime: Runtime, client: httpx.AsyncClient
    ) -> None:
        session = runtime.sessions.create()
        await runtime.orchestrator.handle_turn(session, "Can I double the dose of my lisinopril?")

        assert (await client.get("/api/escalations?category=clinical")).json()
        assert (await client.get("/api/escalations?category=administrative")).json() == []

    async def test_an_unknown_category_is_rejected(self, client: httpx.AsyncClient) -> None:
        assert (await client.get("/api/escalations?category=made-up")).status_code == 422

    async def test_a_refill_request_is_listed_as_pending_review(
        self, runtime: Runtime, client: httpx.AsyncClient
    ) -> None:
        session = runtime.sessions.create()
        await runtime.orchestrator.handle_turn(session, "I need a refill of my lisinopril")
        await runtime.orchestrator.handle_turn(session, IDENTIFY)
        await runtime.orchestrator.handle_turn(session, "yes")

        body = (await client.get("/api/medications/refill-requests")).json()
        assert body
        assert body[0]["status"] == "PENDING_REVIEW"
        assert "isinopril" in body[0]["medication_display"]


class TestRecords:
    async def test_the_roster_is_searchable_and_labelled_synthetic(
        self, client: httpx.AsyncClient
    ) -> None:
        body = (await client.get("/api/patients", params={"query": "Smith"})).json()
        assert [p["full_name"] for p in body] == ["John Smith"]
        assert all(p["synthetic"] is True for p in body)

    async def test_patient_detail_returns_the_chart(self, client: httpx.AsyncClient) -> None:
        body = (await client.get("/api/patients/demo-john-smith")).json()
        assert body["patient"]["full_name"] == "John Smith"
        assert body["medications"]
        assert body["conditions"]

    async def test_dosage_text_is_passed_through_untouched(
        self, client: httpx.AsyncClient, memory_ehr: EHRProvider
    ) -> None:
        """The API is a window onto the record, not an interpreter of it."""
        from_ehr = await memory_ehr.get_medications("Patient/demo-john-smith")
        body = (await client.get("/api/patients/demo-john-smith")).json()
        assert {
            m["medication_request_id"]: m["dosage_instruction"] for m in body["medications"]
        } == {m.medication_request_id: m.dosage_instruction for m in from_ehr}

    async def test_an_unknown_patient_is_a_404(self, client: httpx.AsyncClient) -> None:
        assert (await client.get("/api/patients/nobody")).status_code == 404

    async def test_the_schedule_is_returned_for_a_window(self, client: httpx.AsyncClient) -> None:
        body = (await client.get("/api/appointments", params=WINDOW)).json()
        assert body
        assert all(a["duration_minutes"] > 0 for a in body)
        assert body == sorted(body, key=lambda a: a["start"])

    async def test_an_inverted_window_is_rejected(self, client: httpx.AsyncClient) -> None:
        response = await client.get(
            "/api/appointments",
            params={"start_date": WINDOW["end_date"], "end_date": WINDOW["start_date"]},
        )
        assert response.status_code == 422

    async def test_practitioners_are_listed(self, client: httpx.AsyncClient) -> None:
        body = (await client.get("/api/providers")).json()
        assert len(body) == 3
        assert all(p["display_name"].startswith("Dr.") for p in body)

    async def test_the_clinic_says_it_is_fictional(self, client: httpx.AsyncClient) -> None:
        assert "Synthetic" in (await client.get("/api/clinic")).json()["disclaimer"]


class TestOverviewAndUsage:
    async def test_the_overview_counts_what_happened(
        self, runtime: Runtime, client: httpx.AsyncClient
    ) -> None:
        await a_completed_call(runtime)

        body = (await client.get("/api/overview")).json()
        assert body["calls"] == 1
        assert body["turns"] == 2
        assert body["average_turn_ms"] > 0
        assert body["turns_by_intent"]
        assert body["actions"]["appointment.read"] >= 1
        assert body["synthetic_data_only"] is True

    async def test_the_overview_counts_refusals(
        self, runtime: Runtime, client: httpx.AsyncClient
    ) -> None:
        session = runtime.sessions.create()
        await runtime.orchestrator.handle_turn(session, "Can I double the dose of my lisinopril?")

        body = (await client.get("/api/overview")).json()
        assert body["refused_turns"] == 1
        assert body["escalations"] == 1

    async def test_spend_is_zero_with_mock_providers(
        self, runtime: Runtime, client: httpx.AsyncClient
    ) -> None:
        await a_completed_call(runtime)

        usage = (await client.get("/api/usage/summary")).json()
        assert usage["estimated_project_cost_usd"] == "0"
        assert usage["can_spend_money"] is False
        assert usage["status"] == "ok"

    async def test_the_dashboard_reads_the_persisted_ledger(
        self, client: httpx.AsyncClient
    ) -> None:
        """Not the in-process one: the ledger has to survive a restart (COSTS.md)."""
        usage = (await client.get("/api/usage/summary")).json()
        assert usage["project_limit_usd"] == "20"


class TestWithoutPersistence:
    async def test_the_read_apis_say_why_they_are_empty(self, memory_ehr: EHRProvider) -> None:
        app = create_app()
        app.dependency_overrides[get_ehr] = lambda: memory_ehr
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            response = await http.get("/api/calls")
        assert response.status_code == 503
        assert "PERSISTENCE_ENABLED" in response.json()["detail"]
