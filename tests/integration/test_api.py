"""API surface: liveness and system status."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app()) as test_client:
        yield test_client


def test_health_is_ok_and_touches_no_dependency(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "careline-ai-backend"


def test_system_status_reports_configuration(client: TestClient) -> None:
    body = client.get("/api/system/status").json()

    assert body["status"] == "ok"
    assert body["synthetic_data_only"] is True
    assert body["ehr"]["provider"] == "memory"
    assert body["ehr"]["reachable"] is True


def test_system_status_confirms_the_zero_cost_default(client: TestClient) -> None:
    ai = client.get("/api/system/status").json()["ai"]
    assert ai["mode"] == "mock"
    assert ai["can_spend_money"] is False
    assert ai["paid_providers_selected"] == []


def test_system_status_reports_budget_headroom(client: TestClient) -> None:
    budget = client.get("/api/system/status").json()["budget"]
    assert budget["status"] in {"ok", "warn", "blocked"}
    assert budget["project_limit_usd"] == "20"
    assert budget["override_active"] is False


def test_every_response_carries_a_trace_id(client: TestClient) -> None:
    assert client.get("/health").headers["X-Trace-Id"]


def test_supplied_trace_id_is_propagated(client: TestClient) -> None:
    response = client.get("/health", headers={"X-Trace-Id": "trace-abc-123"})
    assert response.headers["X-Trace-Id"] == "trace-abc-123"


def test_startup_seeds_the_in_memory_ehr(client: TestClient) -> None:
    """Memory mode seeds in-process, so the API has data the moment it is up."""
    from app.ehr.factory import get_default_memory_store

    store = get_default_memory_store()
    assert store.count("Patient") >= 5
    assert store.count("Slot") > 0


class TestAgentEndpoints:
    """The dev chat endpoint: the same runtime the CLI and voice agent use."""

    def test_a_conversation_over_http(self, client: TestClient) -> None:
        session_id = client.post("/api/agent/sessions", json={}).json()["session_id"]

        opening = client.post(
            f"/api/agent/sessions/{session_id}/turns",
            json={"utterance": "Are you open on Saturday?"},
        )
        assert opening.status_code == 200
        body = opening.json()
        assert "closed on Saturday and Sunday" in body["message"]
        assert body["trace"]["intent"] == "clinic_faq"
        assert body["session"]["verification"] == "UNVERIFIED"

    def test_the_trace_is_returned_with_each_turn(self, client: TestClient) -> None:
        session_id = client.post("/api/agent/sessions", json={}).json()["session_id"]
        body = client.post(
            f"/api/agent/sessions/{session_id}/turns",
            json={"utterance": "My blood pressure medicine makes me dizzy. Should I take half?"},
        ).json()

        trace = body["trace"]
        assert trace["safety_outcome"] == "REFUSE_AND_ESCALATE"
        assert trace["safety_category"] == "dose_modification"
        assert trace["safety_rule"] == "medication.dose_modification"
        assert trace["escalation_id"]
        assert trace["timings"]["total_ms"] >= 0

    def test_session_history_accumulates(self, client: TestClient) -> None:
        session_id = client.post("/api/agent/sessions", json={}).json()["session_id"]
        for utterance in ("Are you open on Saturday?", "Where are you located?"):
            client.post(f"/api/agent/sessions/{session_id}/turns", json={"utterance": utterance})

        detail = client.get(f"/api/agent/sessions/{session_id}").json()
        assert len(detail["traces"]) == 2
        assert [t["turn_number"] for t in detail["traces"]] == [1, 2]

    def test_an_unknown_session_is_a_404(self, client: TestClient) -> None:
        response = client.post("/api/agent/sessions/sess-nope/turns", json={"utterance": "hello"})
        assert response.status_code == 404

    def test_an_ended_session_refuses_further_turns(self, client: TestClient) -> None:
        session_id = client.post("/api/agent/sessions", json={}).json()["session_id"]
        client.post(f"/api/agent/sessions/{session_id}/end")
        response = client.post(
            f"/api/agent/sessions/{session_id}/turns", json={"utterance": "hello"}
        )
        assert response.status_code == 409

    def test_an_empty_utterance_is_rejected(self, client: TestClient) -> None:
        session_id = client.post("/api/agent/sessions", json={}).json()["session_id"]
        response = client.post(f"/api/agent/sessions/{session_id}/turns", json={"utterance": ""})
        assert response.status_code == 422
