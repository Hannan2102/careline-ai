"""Deterministic LLM provider.

A first-class implementation, not a test double bolted on afterwards: it is
what development and CI use, which is what keeps the provider interface honest
(docs/provider-abstraction.md). It costs nothing and returns the same answer
every time, so a workflow test never depends on a model's mood.

It reports usage like any other provider -- at zero cost -- so the metering
path is exercised by the test suite rather than only in production.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.ai.providers.base import (
    LLMRequest,
    LLMResponse,
    LLMUsage,
    ToolCallRequest,
    ToolCallResponse,
    Transcript,
    VoiceSpec,
)
from app.ai.usage import INPUT_TOKENS, OUTPUT_TOKENS, REQUESTS, UsageLedger


def _approx_tokens(text: str) -> int:
    """Rough token count. Good enough for exercising the metering path."""
    return max(1, len(text) // 4)


class MockLLMProvider:
    """Echoes the last message. Deterministic, free, and offline."""

    name = "mock"

    def __init__(self, ledger: UsageLedger | None = None) -> None:
        self.ledger = ledger

    async def generate(self, request: LLMRequest) -> LLMResponse:
        prompt = request.messages[-1].content if request.messages else ""
        usage = LLMUsage(
            input_tokens=sum(_approx_tokens(m.content) for m in request.messages),
            output_tokens=_approx_tokens(prompt),
        )
        self._meter(usage, request.session_id)
        # Returns the text unchanged: in mock mode the deterministic template
        # from the workflow is already the response.
        return LLMResponse(text=prompt, usage=usage, finish_reason="stop")

    async def tool_call(self, request: ToolCallRequest) -> ToolCallResponse:
        """Proposes nothing.

        Tool selection in mock mode is the orchestrator's deterministic
        routing; a scripted tool call here would be theatre.
        """
        usage = LLMUsage(
            input_tokens=sum(_approx_tokens(m.content) for m in request.messages),
            output_tokens=0,
        )
        self._meter(usage, request.session_id)
        return ToolCallResponse(text=None, tool_calls=[], usage=usage, finish_reason="stop")

    def _meter(self, usage: LLMUsage, session_id: str | None) -> None:
        if self.ledger is None:
            return
        self.ledger.record(self.name, INPUT_TOKENS, usage.input_tokens, session_id)
        self.ledger.record(self.name, OUTPUT_TOKENS, usage.output_tokens, session_id)
        self.ledger.record(self.name, REQUESTS, 1, session_id)


class MockSTTProvider:
    """Yields whatever was queued. Used by voice tests without audio."""

    name = "mock"

    def __init__(self, transcripts: tuple[str, ...] = ()) -> None:
        self.transcripts = transcripts

    async def transcribe_stream(self, audio: AsyncIterator[bytes]) -> AsyncIterator[Transcript]:
        async for _chunk in audio:
            break
        for text in self.transcripts:
            yield Transcript(text=text, is_final=True, confidence=1.0, audio_seconds=0.0)


class MockTTSProvider:
    """Emits silence-shaped bytes so the pipeline can be exercised offline."""

    name = "mock"

    async def synthesize_stream(self, text: str, voice: VoiceSpec) -> AsyncIterator[bytes]:
        for _ in range(max(1, len(text) // 64)):
            yield b"\x00" * 320
