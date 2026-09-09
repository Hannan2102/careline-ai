"""LocalFHIRProvider against a real HAPI FHIR server.

Marked ``integration``: skipped unless a server is reachable, so the default
suite stays offline and free.

    make up && make wait-fhir && make test-int

These assertions mirror the in-memory provider's contract on purpose -- the
point of the abstraction is that both behave identically (ADR 001).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, timedelta

import pytest
from tests.conftest import JOHN_SMITH, JOHN_SMITH_DOB

from app.config.settings import get_settings
from app.ehr.base import EHRConflictError
from app.ehr.local_fhir import LocalFHIRProvider
from app.ehr.seeding import seed_fhir_server
from app.fhir.client import FhirClient
from app.schemas.domain import AppointmentStatus, AppointmentType

pytestmark = pytest.mark.integration


@pytest.fixture
async def fhir_client() -> AsyncIterator[FhirClient]:
    settings = get_settings()
    client = FhirClient(settings.fhir_base_url, timeout=settings.fhir_timeout_seconds)
    if not await client.ping():
        await client.aclose()
        pytest.skip(f"no FHIR server at {settings.fhir_base_url}")
    yield client
    await client.aclose()


@pytest.fixture
async def hapi(fhir_client: FhirClient) -> AsyncIterator[LocalFHIRProvider]:
    await seed_fhir_server(fhir_client)
    yield LocalFHIRProvider(fhir_client)


async def test_metadata_reports_fhir_r4(fhir_client: FhirClient) -> None:
    statement = await fhir_client.capability_statement()
    assert statement["resourceType"] == "CapabilityStatement"
    assert str(statement.get("fhirVersion", "")).startswith("4.")


async def test_patient_search_round_trips(hapi: LocalFHIRProvider) -> None:
    matches = await hapi.search_patients("John Smith", JOHN_SMITH_DOB)
    assert [p.reference for p in matches] == [JOHN_SMITH]


async def test_unknown_patient_returns_nothing(hapi: LocalFHIRProvider) -> None:
    assert await hapi.search_patients("Jane Doe", date(1970, 1, 1)) == []


async def test_dosage_is_returned_verbatim_from_the_server(hapi: LocalFHIRProvider) -> None:
    found = await hapi.get_medication_request(JOHN_SMITH, "metformin")
    assert found is not None
    assert found.dosage_instruction == "One tablet twice daily with meals"


async def test_booking_and_cancelling_round_trips(hapi: LocalFHIRProvider) -> None:
    today = date.today()
    slots = await hapi.get_available_slots(
        AppointmentType.SICK_VISIT, today + timedelta(days=1), today + timedelta(days=10), limit=5
    )
    assert slots, "expected availability after seeding"

    appointment = await hapi.book_appointment(
        JOHN_SMITH, slots[0].slot_id, AppointmentType.SICK_VISIT, "Sore throat"
    )
    assert appointment.status is AppointmentStatus.BOOKED

    booked = await hapi.get_appointments(JOHN_SMITH)
    assert appointment.appointment_id in [a.appointment_id for a in booked]

    cancelled = await hapi.cancel_appointment(appointment.appointment_id)
    assert cancelled.status is AppointmentStatus.CANCELLED

    free_again = await hapi.get_available_slots(
        AppointmentType.SICK_VISIT,
        today + timedelta(days=1),
        today + timedelta(days=10),
        practitioner_ref=slots[0].practitioner_ref,
        limit=50,
    )
    assert slots[0].start in [s.start for s in free_again]


async def test_double_booking_is_refused_by_the_server(hapi: LocalFHIRProvider) -> None:
    today = date.today()
    slots = await hapi.get_available_slots(
        AppointmentType.FOLLOW_UP, today + timedelta(days=1), today + timedelta(days=10), limit=5
    )
    other = await hapi.create_patient("Casey", "Lin", date(1988, 9, 9))
    await hapi.book_appointment(JOHN_SMITH, slots[0].slot_id, AppointmentType.FOLLOW_UP)

    with pytest.raises(EHRConflictError):
        await hapi.book_appointment(other.reference, slots[0].slot_id, AppointmentType.FOLLOW_UP)
