"""Validating what the model proposed.

A tool call arrives as a name and a bag of JSON. Between that and anything
happening sits this module: the arguments are parsed against a Pydantic model,
and a proposal that does not parse is **never executed** -- it is sent back for
repair, and after a bounded number of attempts the conversation escalates
(docs/agent-tools.md).

Three rules this enforces, each of which is a way a model gets a patient hurt:

* no coercion -- a missing required field is a failure, not a default;
* no partial execution -- a call is valid in full or not at all;
* no patient the session has not verified -- a ``patient_id`` the model
  supplies is checked against the session's own reference, so guessing an id
  reaches nobody (ADR 003).

The repair loop is bounded because an unbounded one is a way to spend money:
each retry is another paid request.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from app.ai.providers.base import ChatMessage, ProposedToolCall
from app.observability.logging import get_logger

logger = get_logger(__name__)

#: Two repairs, then stop. Chosen to match docs/agent-tools.md; the cost of
#: another attempt is a paid request and a second of latency.
MAX_REPAIR_ATTEMPTS = 2

ToolErrorCode = Literal[
    "INVALID_ARGUMENTS",
    "UNKNOWN_TOOL",
    "NOT_VERIFIED",
    "PATIENT_MISMATCH",
]


class ValidatedToolCall(BaseModel):
    """A proposal that parsed. Carries the typed model, not the raw JSON."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    tool_call_id: str
    name: str
    arguments: BaseModel


class ToolCallRejection(BaseModel):
    """Why a proposal was refused, in terms the model can act on."""

    model_config = ConfigDict(frozen=True)

    tool_call_id: str
    name: str
    code: ToolErrorCode
    message: str

    def as_repair_message(self) -> ChatMessage:
        """The correction sent back to the model.

        Says what was wrong and nothing else. Restating the arguments would
        put rejected values back into the context, where the next attempt can
        copy them.
        """
        return ChatMessage(
            role="tool",
            tool_call_id=self.tool_call_id,
            name=self.name,
            content=f"{self.code}: {self.message}",
        )


class ToolRegistry:
    """Names to argument models. The complete callable surface.

    A tool absent from here cannot be called however convincingly the model
    asks for it, which is the property that makes the catalogue in
    docs/agent-tools.md a boundary rather than a description.
    """

    def __init__(self, schemas: dict[str, type[BaseModel]] | None = None) -> None:
        self._schemas: dict[str, type[BaseModel]] = dict(schemas or {})

    def register(self, name: str, model: type[BaseModel]) -> None:
        self._schemas[name] = model

    def known(self, name: str) -> bool:
        return name in self._schemas

    def model_for(self, name: str) -> type[BaseModel] | None:
        return self._schemas.get(name)

    @property
    def names(self) -> list[str]:
        return sorted(self._schemas)


def validate_tool_call(
    proposal: ProposedToolCall,
    registry: ToolRegistry,
    verified_patient_ref: str | None = None,
) -> ValidatedToolCall | ToolCallRejection:
    """Turn a proposal into something executable, or explain why not."""
    model = registry.model_for(proposal.name)
    if model is None:
        return ToolCallRejection(
            tool_call_id=proposal.tool_call_id,
            name=proposal.name,
            code="UNKNOWN_TOOL",
            message=f"No such tool. Available tools: {', '.join(registry.names)}.",
        )

    try:
        arguments = model.model_validate(proposal.arguments)
    except ValidationError as exc:
        return ToolCallRejection(
            tool_call_id=proposal.tool_call_id,
            name=proposal.name,
            code="INVALID_ARGUMENTS",
            message=_describe(exc),
        )

    patient_id = getattr(arguments, "patient_id", None)
    if patient_id is not None:
        rejection = _check_patient(proposal, str(patient_id), verified_patient_ref)
        if rejection is not None:
            return rejection

    return ValidatedToolCall(
        tool_call_id=proposal.tool_call_id, name=proposal.name, arguments=arguments
    )


def _check_patient(
    proposal: ProposedToolCall, patient_id: str, verified_patient_ref: str | None
) -> ToolCallRejection | None:
    """Refuse a patient the session has not verified.

    The message deliberately does not say whether the requested patient
    exists: a rejection that distinguishes "not yours" from "not real" is a
    lookup oracle for anyone who can talk to the agent.
    """
    if verified_patient_ref is None:
        logger.warning("tool_call_refused_unverified", tool=proposal.name)
        return ToolCallRejection(
            tool_call_id=proposal.tool_call_id,
            name=proposal.name,
            code="NOT_VERIFIED",
            message="This session has no verified patient. Verify identity first.",
        )

    requested = patient_id if patient_id.startswith("Patient/") else f"Patient/{patient_id}"
    if requested != verified_patient_ref:
        logger.warning("tool_call_refused_patient_mismatch", tool=proposal.name)
        return ToolCallRejection(
            tool_call_id=proposal.tool_call_id,
            name=proposal.name,
            code="PATIENT_MISMATCH",
            message=(
                "That patient id does not belong to this session. Use the verified "
                "patient for this conversation."
            ),
        )
    return None


def _describe(error: ValidationError) -> str:
    """Field-level detail, without echoing the values that were rejected."""
    parts = []
    for item in error.errors()[:5]:
        location = ".".join(str(piece) for piece in item["loc"]) or "(root)"
        parts.append(f"{location}: {item['msg']}")
    return "; ".join(parts) or "arguments did not validate"


class RepairBudget:
    """Counts repair attempts so the loop cannot run forever.

    Per proposal rather than per conversation: one badly-formed call should
    not consume the allowance of a later, unrelated one.
    """

    def __init__(self, max_attempts: int = MAX_REPAIR_ATTEMPTS) -> None:
        self.max_attempts = max_attempts
        self._used: dict[str, int] = {}

    def may_retry(self, tool_call_id: str) -> bool:
        return self._used.get(tool_call_id, 0) < self.max_attempts

    def record_attempt(self, tool_call_id: str) -> int:
        self._used[tool_call_id] = self._used.get(tool_call_id, 0) + 1
        return self._used[tool_call_id]

    def exhausted(self, tool_call_id: str) -> bool:
        return not self.may_retry(tool_call_id)


def repair_messages(rejections: list[ToolCallRejection]) -> list[ChatMessage]:
    """The turn to send back so the model can correct itself."""
    return [rejection.as_repair_message() for rejection in rejections]


__all__ = [
    "MAX_REPAIR_ATTEMPTS",
    "RepairBudget",
    "ToolCallRejection",
    "ToolRegistry",
    "ValidatedToolCall",
    "repair_messages",
    "validate_tool_call",
]
