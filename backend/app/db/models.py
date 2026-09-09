"""Application schema tables.

Deliberately absent: demographics, dosage text, and clinical content. The audit
row records *that* a medication list was read and for which patient reference,
never what it contained -- copying clinical data into a second store widens the
blast radius without improving the record.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SessionRow(Base):
    """One conversation."""

    __tablename__ = "session"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    verification: Mapped[str] = mapped_column(String(32), nullable=False)
    patient_ref: Mapped[str | None] = mapped_column(String(128), index=True)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    turn_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TurnRow(Base):
    """One exchange, with everything needed to render an agent trace."""

    __tablename__ = "turn"

    turn_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("session.session_id"), nullable=False, index=True
    )
    turn_number: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    utterance: Mapped[str] = mapped_column(Text, nullable=False)
    response: Mapped[str] = mapped_column(Text, nullable=False)

    safety_outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    safety_category: Mapped[str | None] = mapped_column(String(64))
    safety_rule: Mapped[str | None] = mapped_column(String(128))

    intent: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    #: Entity *names* and non-identifying values; identifiers are redacted
    #: before they reach this column.
    entities: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)

    workflow: Mapped[str | None] = mapped_column(String(64), index=True)
    workflow_state: Mapped[str | None] = mapped_column(String(64))
    workflow_status: Mapped[str | None] = mapped_column(String(32))
    escalation_id: Mapped[str | None] = mapped_column(String(64))
    verification_state: Mapped[str] = mapped_column(String(32), nullable=False)

    safety_ms: Mapped[float] = mapped_column(Float, default=0.0)
    extraction_ms: Mapped[float] = mapped_column(Float, default=0.0)
    workflow_ms: Mapped[float] = mapped_column(Float, default=0.0)
    total_ms: Mapped[float] = mapped_column(Float, default=0.0)
    stt_ms: Mapped[float | None] = mapped_column(Float)
    tts_first_audio_ms: Mapped[float | None] = mapped_column(Float)

    estimated_cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), default=0)

    __table_args__ = (Index("ix_turn_session_number", "session_id", "turn_number"),)


class AuditEventRow(Base):
    """Append-only record of access and mutation."""

    __tablename__ = "audit_event"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    session_id: Mapped[str | None] = mapped_column(String(64), index=True)
    #: The turn that produced this event. Set by the persistence flush, which
    #: writes each turn's new events together; it is what lets the Agent Trace
    #: page show the record operations belonging to one exchange.
    turn_id: Mapped[str | None] = mapped_column(String(64), index=True)
    patient_ref: Mapped[str | None] = mapped_column(String(128), index=True)
    resource_type: Mapped[str | None] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(128))
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class EscalationRow(Base):
    """A structured handoff to a human."""

    __tablename__ = "escalation"

    escalation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    priority: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    destination: Mapped[str] = mapped_column(String(64), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    patient_ref: Mapped[str | None] = mapped_column(String(128), index=True)
    verification_state: Mapped[str] = mapped_column(String(32), nullable=False)
    medication_display: Mapped[str | None] = mapped_column(String(200))
    #: The patient's own words. Needed for a clinician to act on the handoff.
    patient_question: Mapped[str | None] = mapped_column(Text)
    ai_action: Mapped[str] = mapped_column(String(200), nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RefillRequestRow(Base):
    """A refill awaiting clinician review. Never an authorisation (FHIR.md)."""

    __tablename__ = "refill_request"

    refill_request_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    patient_ref: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    medication_request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    medication_display: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(64), index=True)


class ProviderUsageRow(Base):
    """Metered provider usage, priced at record time (COSTS.md)."""

    __tablename__ = "provider_usage"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    session_id: Mapped[str | None] = mapped_column(String(64), index=True)
    metric: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[float] = mapped_column(Numeric(16, 4), nullable=False)
    estimated_cost: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
