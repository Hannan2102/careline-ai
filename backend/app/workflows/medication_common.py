"""Shared pieces for the medication workflows."""

from __future__ import annotations

from app.workflows.base import WorkflowMemory


class MedicationMemory(WorkflowMemory):
    """Namespaced scratch space for the medication workflows.

    A distinct type so the two workflows read as siblings rather than as one
    workflow reaching into the other's state.
    """
