#!/usr/bin/env python3
"""Block until the FHIR server answers /metadata, or time out."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.config.settings import get_settings  # noqa: E402
from app.fhir.client import FhirClient  # noqa: E402

TIMEOUT_SECONDS = 180


async def main() -> int:
    settings = get_settings()
    deadline = time.monotonic() + TIMEOUT_SECONDS
    print(f"Waiting for FHIR at {settings.fhir_base_url} (up to {TIMEOUT_SECONDS}s) ...")

    while time.monotonic() < deadline:
        client = FhirClient(settings.fhir_base_url, timeout=5.0)
        try:
            if await client.ping():
                statement = await client.capability_statement()
                print(
                    f"OK: {statement.get('software', {}).get('name', 'FHIR server')} "
                    f"| fhirVersion {statement.get('fhirVersion', 'unknown')}"
                )
                return 0
        finally:
            await client.aclose()
        await asyncio.sleep(3)

    print(f"TIMEOUT: no response from {settings.fhir_base_url}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
