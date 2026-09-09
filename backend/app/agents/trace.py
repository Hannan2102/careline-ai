"""Per-turn trace.

What the dashboard's Agent Trace page renders (Phase 10), and what makes a
misbehaving turn diagnosable without a debugger. It exists for debugging first
and demo second.

Captured for every turn: what was said, what the safety layer decided and which
rule fired, what was extracted, which workflow ran, what it did to the record,
what was said back, how long each stage took, and what it cost.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.agents.intents import Intent
from app.safety.models import SafetyCategory, SafetyOutcome
from app.schemas.domain import AuditEvent


class StageTimings(BaseModel):
    """Milliseconds per stage.

    Voice adds STT and TTS around these (Phase 13). Recording them from the
    start means the latency budget in docs/latency.md is measured rather than
    estimated when it starts to matter.
    """

    model_config = ConfigDict(frozen=True)

    safety_ms: float = 0.0
    extraction_ms: float = 0.0
    workflow_ms: float = 0.0
    total_ms: float = 0.0
    stt_ms: float | None = None
    tts_first_audio_ms: float | None = None


class TurnTrace(BaseModel):
    """One exchange, recorded end to end."""

    model_config = ConfigDict(frozen=True)

    turn_id: str = Field(default_factory=lambda: f"turn-{uuid.uuid4().hex[:12]}")
    session_id: str
    turn_number: int
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    utterance: str
    response: str

    safety_outcome: SafetyOutcome
    safety_category: SafetyCategory | None = None
    safety_rule: str | None = None

    intent: Intent
    confidence: float
    entities: dict[str, str] = {}

    workflow: str | None = None
    workflow_state: str | None = None
    workflow_status: str | None = None

    #: The record operations this turn performed. Audit events are the honest
    #: answer to "what did it actually do", since they are written at the point
    #: of access rather than reconstructed afterwards.
    operations: tuple[AuditEvent, ...] = ()
    escalation_id: str | None = None

    verification_state: str = "UNVERIFIED"
    timings: StageTimings = StageTimings()
    estimated_cost_usd: str = "0"

    @property
    def was_refused(self) -> bool:
        return self.safety_outcome is SafetyOutcome.REFUSE_AND_ESCALATE


class TraceStore:
    """In-process traces. Phase 11 persists to the ``turn`` table."""

    def __init__(self) -> None:
        self._traces: list[TurnTrace] = []

    def append(self, trace: TurnTrace) -> None:
        self._traces.append(trace)

    def all(self) -> list[TurnTrace]:
        return list(self._traces)

    def for_session(self, session_id: str) -> list[TurnTrace]:
        return [t for t in self._traces if t.session_id == session_id]

    def clear(self) -> None:
        self._traces.clear()
