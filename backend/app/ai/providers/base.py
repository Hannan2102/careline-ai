"""AI provider interfaces (ADR 004, docs/provider-abstraction.md).

Business logic imports these Protocols; only the factory imports an
implementation. No vendor SDK may be imported outside
``app/ai/providers/<kind>/``.

Implementations land in Phase 12 (cloud) and Phase 17 (local). The mock
implementations are first-class, not test doubles bolted on afterwards -- they
are what development and CI use, which is what keeps these interfaces honest.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------
# Shared value types
# --------------------------------------------------------------------------


class Transcript(BaseModel):
    """A speech recognition result.

    Only ``is_final`` transcripts reach the orchestrator; interim results are
    displayed and logged but never acted on (docs/voice-architecture.md).
    """

    text: str
    is_final: bool
    confidence: float | None = None
    audio_seconds: float = 0.0


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_call_id: str | None = None
    name: str | None = None


class ToolSpec(BaseModel):
    """A tool offered to the model, described by its JSON schema."""

    name: str
    description: str
    parameters: dict[str, Any]


class LLMRequest(BaseModel):
    messages: list[ChatMessage]
    max_output_tokens: int = Field(default=400, gt=0)
    temperature: float = 0.2
    session_id: str | None = None


class LLMUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


class LLMResponse(BaseModel):
    text: str
    usage: LLMUsage = LLMUsage()
    finish_reason: str | None = None


class ToolCallRequest(LLMRequest):
    tools: list[ToolSpec]
    tool_choice: Literal["auto", "required", "none"] = "auto"


class ProposedToolCall(BaseModel):
    """A tool call the model *proposed*.

    Deliberately not executable: arguments are raw JSON until the tool layer
    validates them against the tool's Pydantic schema (docs/agent-tools.md).
    """

    tool_call_id: str
    name: str
    arguments: dict[str, Any]


class ToolCallResponse(BaseModel):
    text: str | None = None
    tool_calls: list[ProposedToolCall] = []
    usage: LLMUsage = LLMUsage()
    finish_reason: str | None = None


class VoiceSpec(BaseModel):
    voice_id: str | None = None
    model: str | None = None
    speed: float = 1.0


# --------------------------------------------------------------------------
# Provider errors
# --------------------------------------------------------------------------


class ProviderError(Exception):
    """Base class; vendor exceptions are translated at the adapter boundary."""


class ProviderUnavailableError(ProviderError):
    """The provider could not be reached or returned an unusable response."""


class ProviderBudgetBlockedError(ProviderError):
    """A paid call was refused by the budget guard."""


# --------------------------------------------------------------------------
# Interfaces
# --------------------------------------------------------------------------


@runtime_checkable
class STTProvider(Protocol):
    name: str

    # Not `async def`: an async *generator* is a plain function returning an
    # AsyncIterator. Declaring it `async def` makes the Protocol describe a
    # coroutine yielding an iterator, which no implementation here satisfies --
    # a mismatch nothing catches until something is type-checked against it.
    def transcribe_stream(self, audio: AsyncIterator[bytes]) -> AsyncIterator[Transcript]:
        """Stream audio in, yield interim and final transcripts out."""
        ...


@runtime_checkable
class LLMProvider(Protocol):
    name: str

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Plain text completion, used for wording an already-decided response."""
        ...

    async def tool_call(self, request: ToolCallRequest) -> ToolCallResponse:
        """Propose tool calls. The proposal is validated before anything executes."""
        ...


@runtime_checkable
class TTSProvider(Protocol):
    name: str

    def synthesize_stream(self, text: str, voice: VoiceSpec) -> AsyncIterator[bytes]:
        """Stream synthesised audio, starting at the first sentence boundary."""
        ...
