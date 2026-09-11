"""Workflow foundations.

Each workflow is an explicit state machine over ``SessionState`` (ADR 002).
States are named, transitions are testable, and a half-finished booking is a
serialisable value rather than a hope about what a model remembers.

Workflows are deterministic. They take *typed input*, not free text: turning an
utterance into those fields is the orchestrator's job (Phase 9), and keeping
that boundary means every workflow can be tested without a model.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.schemas.domain import Appointment
from app.utils.formatting import format_appointment, format_slot


class WorkflowStatus(StrEnum):
    """Whether the workflow needs more from the caller."""

    AWAITING_INPUT = "AWAITING_INPUT"
    COMPLETED = "COMPLETED"
    ESCALATED = "ESCALATED"


class AwaitedInput(StrEnum):
    """What the workflow needs next.

    The orchestrator uses this to decide what to extract from the next
    utterance, so the model is asked a narrow question rather than left to
    infer the state of the conversation.
    """

    IDENTITY = "identity"
    SECOND_FACTOR = "second_factor"
    REASON = "reason"
    SLOT_CHOICE = "slot_choice"
    MEDICATION_CHOICE = "medication_choice"
    CONFIRMATION = "confirmation"


class SlotOffer(BaseModel):
    """One appointment time offered to the patient.

    Carries its own ordinal so the patient can say "the second one" and the
    orchestrator can resolve that without re-deriving the list.
    """

    model_config = ConfigDict(frozen=True)

    index: int
    slot_id: str
    practitioner_ref: str
    practitioner_name: str
    start: datetime
    duration_minutes: int

    @property
    def label(self) -> str:
        return format_appointment(self.start, self.practitioner_name)

    @property
    def short_label(self) -> str:
        return format_slot(self.start)


class WorkflowResponse(BaseModel):
    """The result of advancing a workflow by one turn."""

    model_config = ConfigDict(frozen=True)

    workflow: str
    state: str
    status: WorkflowStatus
    #: Deterministic wording. The model may rephrase it in Phase 9; it is also
    #: the fallback when there is no model, which is what makes text mode free.
    message: str
    awaiting: AwaitedInput | None = None
    offers: tuple[SlotOffer, ...] = ()
    appointment: Appointment | None = None
    escalation_id: str | None = None

    @property
    def is_complete(self) -> bool:
        return self.status is WorkflowStatus.COMPLETED

    @property
    def is_escalated(self) -> bool:
        return self.status is WorkflowStatus.ESCALATED


class WorkflowMemory:
    """Typed access to a workflow's slice of ``session.workflow_state``.

    Namespaced by workflow so two workflows cannot collide, and deliberately
    plain data: this is scratch space, and it never carries authorisation.
    """

    def __init__(self, store: dict[str, Any], namespace: str) -> None:
        self._store = store
        self._namespace = namespace

    def _key(self, name: str) -> str:
        return f"{self._namespace}.{name}"

    def get(self, name: str, default: Any = None) -> Any:
        return self._store.get(self._key(name), default)

    def set(self, name: str, value: Any) -> None:
        self._store[self._key(name)] = value

    def clear(self) -> None:
        for key in [k for k in self._store if k.startswith(f"{self._namespace}.")]:
            del self._store[key]
