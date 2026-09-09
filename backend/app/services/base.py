"""Service-layer errors.

Services raise these; the tool layer converts them into the typed failures the
agent sees (docs/agent-tools.md). Keeping the taxonomies aligned means a new
failure mode has an obvious conversational response rather than becoming an
unhandled exception the agent has to narrate.
"""

from __future__ import annotations


class ServiceError(Exception):
    """Base class for every service-layer failure."""


class NotFoundError(ServiceError):
    """The requested record does not exist."""


class ConflictError(ServiceError):
    """The request cannot be satisfied in the current state (e.g. slot taken)."""


class ValidationError(ServiceError):
    """Caller-supplied data is invalid. Never raised from model output directly."""


class NotOwnedError(ServiceError):
    """The record exists but does not belong to this patient.

    Kept distinct from ``NotFoundError`` internally so the audit trail records
    what actually happened. The *response* to the patient must not distinguish
    the two -- confirming that an appointment exists for someone else is a
    disclosure (SAFETY.md).
    """


class UpstreamUnavailableError(ServiceError):
    """The EHR could not be reached. Degrade to a human, never to a guess."""
