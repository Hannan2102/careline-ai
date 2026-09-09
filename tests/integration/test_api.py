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
