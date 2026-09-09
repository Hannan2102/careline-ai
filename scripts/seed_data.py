#!/usr/bin/env python3
"""Load synthetic data into the configured EHR.

    python scripts/seed_data.py                 # uses EHR_PROVIDER from .env
    python scripts/seed_data.py --provider local
    python scripts/seed_data.py --days 30 --dry-run

All data is fictional (synthetic-data/README.md).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.config.settings import EHRProviderName, get_settings  # noqa: E402
from app.ehr.seeding import (  # noqa: E402
    DEFAULT_DAYS_AHEAD,
    dataset_report,
    seed_fhir_server,
)
from app.fhir.client import FhirClient  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description="Seed synthetic patient-access data.")
    parser.add_argument("--provider", choices=["local", "memory"], default=None)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS_AHEAD)
    parser.add_argument("--dry-run", action="store_true", help="Print the dataset, write nothing.")
    args = parser.parse_args()

    settings = get_settings()
    provider = args.provider or settings.ehr_provider.value

    print(f"Dataset to be seeded ({args.days} days ahead):\n{dataset_report(args.days)}\n")
    if args.dry_run:
        print("--dry-run: nothing written.")
        return 0

    if provider == EHRProviderName.MEMORY.value:
        # The in-memory store lives inside the API process; a separate script
        # would populate a store nothing else can see.
        print(
            "EHR_PROVIDER=memory seeds automatically when the backend starts.\n"
            "Run 'make dev' and check GET /api/system/status, or use\n"
            "'--provider local' to seed a running HAPI FHIR server."
        )
        return 0

    print(f"Seeding {settings.fhir_base_url} ...")
    client = FhirClient(settings.fhir_base_url, timeout=settings.fhir_timeout_seconds)
    try:
        if not await client.ping():
            print(
                f"ERROR: no FHIR server at {settings.fhir_base_url}.\n"
                "Start it with 'make up && make wait-fhir'.",
                file=sys.stderr,
            )
            return 1
        summary = await seed_fhir_server(client, days_ahead=args.days)
    finally:
        await client.aclose()

    print("Seeded:")
    for key, value in sorted(summary.items()):
        print(f"  {value:>6}  {key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
