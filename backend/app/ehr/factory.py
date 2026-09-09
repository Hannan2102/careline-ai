"""EHR provider selection.

The single place that knows which provider is configured. Everything else
depends on the :class:`~app.ehr.base.EHRProvider` interface (ADR 001).
"""

from __future__ import annotations

from app.config.settings import EHRProviderName, Settings
from app.ehr.base import EHRProvider
from app.ehr.local_fhir import LocalFHIRProvider
from app.ehr.memory_fhir import InMemoryFhirStore, MemoryFHIRProvider
from app.fhir.client import FhirClient

#: Process-wide store so the API, the seed script, and the agent runtime all
#: see the same synthetic data when running without Docker.
_default_memory_store = InMemoryFhirStore()


def get_default_memory_store() -> InMemoryFhirStore:
    return _default_memory_store


def build_ehr_provider(settings: Settings) -> EHRProvider:
    """Construct the configured provider."""
    match settings.ehr_provider:
        case EHRProviderName.MEMORY:
            return MemoryFHIRProvider(store=_default_memory_store)
        case EHRProviderName.LOCAL:
            return LocalFHIRProvider(
                FhirClient(
                    base_url=settings.fhir_base_url,
                    timeout=settings.fhir_timeout_seconds,
                )
            )
        case EHRProviderName.EPIC:
            raise NotImplementedError(
                "EpicFHIRProvider arrives in Phase 16 (see ROADMAP.md). "
                "Use EHR_PROVIDER=local or memory."
            )
    raise ValueError(f"unsupported EHR provider {settings.ehr_provider!r}")
