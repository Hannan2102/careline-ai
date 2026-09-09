"""HAPI-specific checks.

The behavioural contract lives in ``test_ehr_contract.py`` and runs against both
providers. What remains here is what only a real FHIR server can tell us: that
it speaks R4, and that its own concurrency control -- not ours -- refuses a
lost update.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, timedelta

import pytest
from tests.conftest import JOHN_SMITH, SEED_TODAY

from app.config.settings import Settings
from app.ehr.base import EHRConflictError
from app.ehr.local_fhir import LocalFHIRProvider
from app.ehr.seeding import reset_fhir_server
from app.fhir.client import FhirClient
from app.schemas.domain import AppointmentType

pytestmark = pytest.mark.integration


@pytest.fixture
async def fhir_client() -> AsyncIterator[FhirClient]:
    settings = Settings(_env_file=None, app_env="test")
    client = FhirClient(settings.fhir_base_url, timeout=settings.fhir_timeout_seconds)
    if not await client.ping():
        await client.aclose()
        pytest.skip(f"no FHIR server at {settings.fhir_base_url}")
    yield client
    await client.aclose()


async def test_server_reports_fhir_r4(fhir_client: FhirClient) -> None:
    statement = await fhir_client.capability_statement()
    assert statement["resourceType"] == "CapabilityStatement"
    assert str(statement.get("fhirVersion", "")).startswith("4.")


async def test_slot_writes_are_version_checked(fhir_client: FhirClient) -> None:
    """A stale If-Match must be refused, which is what makes booking race-safe.

    Verifies the server's optimistic locking directly, rather than inferring it
    from a booking outcome.
    """
    await reset_fhir_server(fhir_client, today=SEED_TODAY)
    provider = LocalFHIRProvider(fhir_client)
    slots = await provider.get_available_slots(
        AppointmentType.FOLLOW_UP,
        SEED_TODAY + timedelta(days=1),
        SEED_TODAY + timedelta(days=5),
        limit=1,
    )
    assert slots

    slot = await fhir_client.read("Slot", slots[0].slot_id)
    stale_version = slot["meta"]["versionId"]

    slot["status"] = "busy"
    await fhir_client.update(slot, if_match=stale_version)

    # A second write at the same version is the lost update we must prevent.
    slot["status"] = "free"
    with pytest.raises(EHRConflictError):
        await fhir_client.update(slot, if_match=stale_version)


async def test_seeded_resources_are_retrievable_over_rest(
    fhir_client: FhirClient,
) -> None:
    await reset_fhir_server(fhir_client, today=SEED_TODAY)
    patients = await fhir_client.search(
        "Patient", {"family": "Smith", "given": "John", "birthdate": "1985-02-15"}
    )
    assert [f"Patient/{p['id']}" for p in patients] == [JOHN_SMITH]

    medications = await fhir_client.search(
        "MedicationRequest", {"patient": JOHN_SMITH, "status": "active"}
    )
    stored = {
        m["medicationCodeableConcept"]["text"]: m["dosageInstruction"][0]["text"]
        for m in medications
        if m.get("dosageInstruction")
    }
    assert stored["Metformin 500 mg"] == "One tablet twice daily with meals"


async def test_reset_restores_a_known_state(fhir_client: FhirClient) -> None:
    """Demos must start identically every run."""
    provider = LocalFHIRProvider(fhir_client)
    await reset_fhir_server(fhir_client, today=SEED_TODAY)
    before = await provider.get_appointments(JOHN_SMITH)

    slots = await provider.get_available_slots(
        AppointmentType.SICK_VISIT,
        SEED_TODAY + timedelta(days=1),
        SEED_TODAY + timedelta(days=5),
        limit=1,
    )
    await provider.book_appointment(JOHN_SMITH, slots[0].slot_id, AppointmentType.SICK_VISIT)
    await provider.create_patient("Temp", "Record", date(1999, 1, 1))

    await reset_fhir_server(fhir_client, today=SEED_TODAY)
    after = await provider.get_appointments(JOHN_SMITH)

    assert len(after) == len(before) == 1
    assert await provider.search_patients("Temp Record", date(1999, 1, 1)) == []
