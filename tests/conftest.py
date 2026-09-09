"""Shared fixtures.

Every test runs against mock/in-memory components: no network, no Docker, no
paid API call (ADR 005). A test that reaches a vendor is a failing test.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from datetime import date

import pytest

# Set before app modules import settings, so nothing picks up a developer's .env.
os.environ.setdefault("AI_MODE", "mock")
os.environ.setdefault("EHR_PROVIDER", "memory")
os.environ.setdefault("TEXT_ONLY_MODE", "true")
os.environ.setdefault("APP_ENV", "test")

from app.ai.budget_guard import BudgetGuard
from app.ai.usage import UsageLedger
from app.config.settings import Settings
from app.ehr.memory_fhir import InMemoryFhirStore, MemoryFHIRProvider
from app.ehr.seeding import seed_memory_store

#: Fixed reference date so slot arithmetic is deterministic. A Tuesday.
SEED_TODAY = date(2026, 9, 8)

JOHN_SMITH = "Patient/demo-john-smith"
JOHN_SMITH_DOB = date(1985, 2, 15)


@pytest.fixture
def settings() -> Settings:
    """Default settings, isolated from any .env on disk."""
    return Settings(_env_file=None, app_env="test")


@pytest.fixture
def ledger() -> UsageLedger:
    return UsageLedger()


@pytest.fixture
def guard(settings: Settings, ledger: UsageLedger) -> BudgetGuard:
    return BudgetGuard(settings, ledger)


@pytest.fixture
def store() -> InMemoryFhirStore:
    return InMemoryFhirStore()


@pytest.fixture
async def seeded_store(store: InMemoryFhirStore) -> AsyncIterator[InMemoryFhirStore]:
    await seed_memory_store(store, today=SEED_TODAY)
    yield store


@pytest.fixture
async def ehr(seeded_store: InMemoryFhirStore) -> AsyncIterator[MemoryFHIRProvider]:
    provider = MemoryFHIRProvider(store=seeded_store)
    yield provider
    await provider.aclose()


@pytest.fixture
def empty_ehr(store: InMemoryFhirStore) -> Iterator[MemoryFHIRProvider]:
    yield MemoryFHIRProvider(store=store)
