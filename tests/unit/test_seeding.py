"""The synthetic dataset: shape, determinism, and its deliberate fixtures."""

from __future__ import annotations

from datetime import date

from tests.conftest import SEED_TODAY

from app.ehr.memory_fhir import InMemoryFhirStore
from app.ehr.seeding import (
    build_dataset,
    load_curated_resources,
    seed_memory_store,
    upcoming_days,
)


def test_curated_resources_load_and_are_all_synthetic() -> None:
    resources = load_curated_resources()
    types = {r["resourceType"] for r in resources}
    assert {"Patient", "Practitioner", "MedicationRequest", "Schedule", "Location"} <= types
    assert all(r.get("id") for r in resources)


def test_documentation_comments_are_stripped_before_load() -> None:
    """'_comment' keys explain fixtures to readers; they must not reach the EHR."""
    assert all("_comment" not in r for r in load_curated_resources())


def test_slots_start_tomorrow_so_no_demo_offers_a_past_appointment() -> None:
    days = upcoming_days(14, today=SEED_TODAY)
    assert days
    assert min(days) > SEED_TODAY


def test_generated_slots_skip_weekends() -> None:
    assert all(day.weekday() < 5 for day in upcoming_days(21, today=SEED_TODAY))


def test_dataset_is_deterministic_for_a_fixed_date() -> None:
    first = build_dataset(today=SEED_TODAY)
    second = build_dataset(today=SEED_TODAY)
    assert [r["id"] for r in first] == [r["id"] for r in second]


async def test_seeding_is_idempotent() -> None:
    store = InMemoryFhirStore()
    first = await seed_memory_store(store, today=SEED_TODAY)
    second = await seed_memory_store(store, today=SEED_TODAY)
    assert first["Patient"] == second["Patient"]
    assert first["Slot"] == second["Slot"]


async def test_seed_books_the_standing_demo_appointment() -> None:
    """Lookup, cancel, and reschedule demos need an appointment to exist."""
    store = InMemoryFhirStore()
    summary = await seed_memory_store(store, today=SEED_TODAY)
    assert summary["_demo_appointments_booked"] == 1
    assert summary["Appointment"] == 1


async def test_the_ambiguous_patient_fixture_exists(seeded_store: InMemoryFhirStore) -> None:
    """Two patients share a name and DOB, forcing the second-factor path."""
    from app.ehr.memory_fhir import MemoryFHIRProvider

    matches = await MemoryFHIRProvider(seeded_store).search_patients(
        "Robert Johnson", date(1990, 6, 21)
    )
    assert len(matches) == 2


async def test_the_missing_dosage_fixture_exists(seeded_store: InMemoryFhirStore) -> None:
    """A prescription with no instruction text, so the escalation path is testable."""
    from app.ehr.memory_fhir import MemoryFHIRProvider

    medications = await MemoryFHIRProvider(seeded_store).get_medications(
        "Patient/demo-linda-nguyen"
    )
    assert any(not m.has_dosage_on_file for m in medications)
