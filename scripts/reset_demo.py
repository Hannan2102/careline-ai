#!/usr/bin/env python3
"""Reset the demo environment to a known state.

For EHR_PROVIDER=local this cancels every booked Appointment and frees its
slots, then reseeds -- so demos start identically every time without
destroying the Docker volume. For a full wipe, use 'make reset'.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.config.settings import EHRProviderName, get_settings  # noqa: E402
from app.ehr.local_fhir import LocalFHIRProvider  # noqa: E402
from app.ehr.seeding import seed_fhir_server  # noqa: E402
from app.fhir.client import FhirClient  # noqa: E402


async def main() -> int:
    settings = get_settings()
    if settings.ehr_provider is EHRProviderName.MEMORY:
        print("EHR_PROVIDER=memory resets on every backend restart. Nothing to do.")
        return 0

    client = FhirClient(settings.fhir_base_url, timeout=settings.fhir_timeout_seconds)
    provider = LocalFHIRProvider(client)
    try:
        if not await client.ping():
            print(f"ERROR: no FHIR server at {settings.fhir_base_url}", file=sys.stderr)
            return 1

        booked = await client.search("Appointment", {"status": "booked", "_count": "200"})
        for resource in booked:
            await provider.cancel_appointment(resource["id"])
        print(f"Cancelled {len(booked)} appointment(s) and released their slots.")

        summary = await seed_fhir_server(client)
        print("Reseeded:")
        for key, value in sorted(summary.items()):
            print(f"  {value:>6}  {key}")
    finally:
        await client.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
