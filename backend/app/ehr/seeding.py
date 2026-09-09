"""Synthetic data seeding.

One dataset definition, loaded into whichever provider is configured, so the
in-memory store and HAPI hold the *same* resources -- that is what stops the
two providers from quietly diverging (ADR 001).

Everything here is fictional. See synthetic-data/README.md.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from app.config.clinic import PRACTITIONERS
from app.ehr.base import EHRConflictError, EHRNotFoundError
from app.ehr.local_fhir import LocalFHIRProvider, build_seed_bundles
from app.ehr.memory_fhir import InMemoryFhirStore, MemoryFHIRProvider, build_slot_resources
from app.fhir.client import FhirClient
from app.fhir.mappings import is_runtime_patient
from app.fhir.models import FhirResource
from app.schemas.domain import AppointmentType
from app.utils.scheduling import is_working_day

#: repo_root/synthetic-data/fhir/curated
CURATED_DIR = Path(__file__).resolve().parents[3] / "synthetic-data" / "fhir" / "curated"

DEFAULT_DAYS_AHEAD = 14

#: A pre-booked appointment so "when is my appointment?", cancellation, and
#: rescheduling demos have something to work with from a fresh seed.
DEMO_APPOINTMENT_PATIENT = "Patient/demo-john-smith"
DEMO_APPOINTMENT_PRACTITIONER = "Practitioner/prac-sarah-patel"
DEMO_APPOINTMENT_TYPE = AppointmentType.DIABETES_FOLLOW_UP
DEMO_APPOINTMENT_LOCAL_HOUR_UTC = 13  # 09:00 America/New_York during EDT


def load_curated_resources(curated_dir: Path | None = None) -> list[FhirResource]:
    """Read the committed curated FHIR resources."""
    directory = curated_dir or CURATED_DIR
    if not directory.is_dir():
        raise FileNotFoundError(f"curated FHIR directory not found: {directory}")

    resources: list[FhirResource] = []
    for path in sorted(directory.glob("*.json")):
        payload = json.loads(path.read_text())
        if not isinstance(payload, list):
            raise ValueError(f"{path} must contain a JSON array of FHIR resources")
        # '_comment' keys document deliberate fixtures; strip them before load.
        resources.extend({k: v for k, v in r.items() if k != "_comment"} for r in payload)
    return resources


def upcoming_days(days_ahead: int = DEFAULT_DAYS_AHEAD, today: date | None = None) -> list[date]:
    """Working days from tomorrow onward.

    Slots start tomorrow so a demo never offers an appointment in the past.
    """
    start = (today or datetime.now(UTC).date()) + timedelta(days=1)
    return [
        day for day in (start + timedelta(days=n) for n in range(days_ahead)) if is_working_day(day)
    ]


def build_dataset(
    days_ahead: int = DEFAULT_DAYS_AHEAD,
    today: date | None = None,
    curated_dir: Path | None = None,
) -> list[FhirResource]:
    """Curated clinic and patients, plus generated free slots."""
    days = upcoming_days(days_ahead, today)
    return load_curated_resources(curated_dir) + build_slot_resources(days)


def _demo_appointment_slot_id(today: date | None = None) -> str | None:
    """Slot id for the pre-booked demo appointment: Dr. Patel, 09:00, soon."""
    from app.utils.scheduling import slot_id_for

    for day in upcoming_days(7, today):
        start = datetime(day.year, day.month, day.day, DEMO_APPOINTMENT_LOCAL_HOUR_UTC, tzinfo=UTC)
        return slot_id_for(DEMO_APPOINTMENT_PRACTITIONER, start)
    return None


async def _book_demo_appointment(
    provider: MemoryFHIRProvider | LocalFHIRProvider, today: date | None = None
) -> int:
    """Book the standing demo appointment. Returns 1 if booked, 0 otherwise."""
    slot_id = _demo_appointment_slot_id(today)
    if slot_id is None:
        return 0
    try:
        await provider.book_appointment(
            patient_ref=DEMO_APPOINTMENT_PATIENT,
            slot_id=slot_id,
            appointment_type=DEMO_APPOINTMENT_TYPE,
            reason="Diabetes follow-up",
        )
    except (EHRConflictError, EHRNotFoundError):
        # Already seeded, or the slot is taken. Seeding stays idempotent.
        return 0
    return 1


async def seed_memory_store(
    store: InMemoryFhirStore,
    days_ahead: int = DEFAULT_DAYS_AHEAD,
    today: date | None = None,
    reset: bool = True,
) -> dict[str, int]:
    """Load the dataset into an in-memory store. Returns a resource-count summary."""
    if reset:
        store.clear()
    store.put_all(build_dataset(days_ahead, today))
    provider = MemoryFHIRProvider(store=store)
    booked = await _book_demo_appointment(provider, today)
    summary = dict(store.summary)
    summary["_demo_appointments_booked"] = booked
    return summary


async def seed_fhir_server(
    client: FhirClient,
    days_ahead: int = DEFAULT_DAYS_AHEAD,
    today: date | None = None,
) -> dict[str, int]:
    """Load the dataset into a FHIR server via transaction bundles.

    Uses conditional PUT at known ids, so re-running updates in place rather
    than creating duplicates.
    """
    resources = build_dataset(days_ahead, today)
    bundles = build_seed_bundles(resources)
    for bundle in bundles:
        await client.transaction(bundle)

    provider = LocalFHIRProvider(client)
    booked = await _book_demo_appointment(provider, today)

    summary: dict[str, int] = {}
    for resource in resources:
        summary[resource["resourceType"]] = summary.get(resource["resourceType"], 0) + 1
    summary["_bundles_sent"] = len(bundles)
    summary["_demo_appointments_booked"] = booked
    return summary


async def reset_fhir_server(
    client: FhirClient,
    days_ahead: int = DEFAULT_DAYS_AHEAD,
    today: date | None = None,
) -> dict[str, int]:
    """Return a FHIR server to the freshly-seeded state.

    Removes appointments left by earlier runs, then reseeds -- which rewrites
    every Slot back to ``free``. Cheaper and less disruptive than destroying the
    database volume, and it makes demos and integration tests reproducible.
    """
    # Appointments first: a Patient cannot be removed while referenced.
    await client.delete_matching("Appointment", {"status": "booked,cancelled,fulfilled,noshow"})
    # Then patients registered at runtime, so a previous run's "new patient"
    # does not linger and turn a later lookup into a duplicate match. Selected
    # by id prefix rather than by identifier, so records written before the
    # identifier existed are cleaned up too.
    for resource in await client.search("Patient", {"_count": "500"}):
        if is_runtime_patient(str(resource["id"])):
            await client.delete("Patient", str(resource["id"]))
    return await seed_fhir_server(client, days_ahead=days_ahead, today=today)


def dataset_report(days_ahead: int = DEFAULT_DAYS_AHEAD, today: date | None = None) -> str:
    """Human-readable summary, used by the seed script and tests."""
    resources = build_dataset(days_ahead, today)
    counts: dict[str, int] = {}
    for resource in resources:
        counts[resource["resourceType"]] = counts.get(resource["resourceType"], 0) + 1
    lines = [f"{count:>6}  {name}" for name, count in sorted(counts.items())]
    days = upcoming_days(days_ahead, today)
    lines.append(f"{len(days):>6}  working days ({days[0]} .. {days[-1]})" if days else "no days")
    lines.append(f"{len(PRACTITIONERS):>6}  practitioners")
    return "\n".join(lines)
