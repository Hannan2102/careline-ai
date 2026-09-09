"""Cloud provider adapters (Phase 12).

Every test here drives the *real* adapter code against a faked transport. No
request leaves the machine and nothing costs money, but the code under test is
the code that will talk to the vendor -- mocking the adapter itself would
prove only that the mock works.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import httpx
import pytest

from app.ai.providers.base import (
    ChatMessage,
    LLMRequest,
    ProviderUnavailableError,
    ToolCallRequest,
    ToolSpec,
    VoiceSpec,
)
from app.ai.providers.llm.openai import OpenAILLMProvider
from app.ai.providers.stt.deepgram import DeepgramSTTProvider
from app.ai.providers.tts.elevenlabs import ElevenLabsTTSProvider
from app.ai.usage import INPUT_TOKENS, OUTPUT_TOKENS, TTS_CHARACTERS, UsageLedger

ASK = LLMRequest(messages=[ChatMessage(role="user", content="When are you open?")])

COMPLETION = {
    "choices": [{"message": {"content": "We are open 8 to 5."}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 120, "completion_tokens": 30},
}


def openai_client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.openai.test/v1"
    )


def responder(payload: dict[str, Any], status: int = 200) -> Any:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return handle


class TestOpenAIGeneration:
    async def test_the_completion_text_is_returned(self) -> None:
        provider = OpenAILLMProvider(api_key="k", client=openai_client(responder(COMPLETION)))
        response = await provider.generate(ASK)
        assert response.text == "We are open 8 to 5."
        assert response.finish_reason == "stop"

    async def test_usage_comes_from_the_response_not_an_estimate(self) -> None:
        """The guard is only as good as the numbers it reads (ADR 006)."""
        ledger = UsageLedger()
        provider = OpenAILLMProvider(
            api_key="k", ledger=ledger, client=openai_client(responder(COMPLETION))
        )
        response = await provider.generate(ASK)

        assert response.usage.input_tokens == 120
        assert response.usage.output_tokens == 30
        totals = {r.metric: r.quantity for r in ledger.records}
        assert totals[INPUT_TOKENS] == Decimal("120")
        assert totals[OUTPUT_TOKENS] == Decimal("30")

    async def test_spend_is_priced_and_nonzero(self) -> None:
        ledger = UsageLedger()
        provider = OpenAILLMProvider(
            api_key="k", ledger=ledger, client=openai_client(responder(COMPLETION))
        )
        await provider.generate(ASK)
        assert ledger.project_total() > Decimal("0")

    async def test_the_request_carries_the_model_and_token_ceiling(self) -> None:
        seen: dict[str, Any] = {}

        def handle(request: httpx.Request) -> httpx.Response:
            seen.update(json.loads(request.content))
            assert request.headers["Authorization"] == "Bearer secret-key"
            return httpx.Response(200, json=COMPLETION)

        provider = OpenAILLMProvider(
            api_key="secret-key", model="gpt-4.1-mini", client=openai_client(handle)
        )
        await provider.generate(LLMRequest(messages=ASK.messages, max_output_tokens=64))
        assert seen["model"] == "gpt-4.1-mini"
        assert seen["max_completion_tokens"] == 64

    async def test_an_api_error_becomes_a_typed_provider_error(self) -> None:
        provider = OpenAILLMProvider(
            api_key="k",
            client=openai_client(responder({"error": {"message": "bad request"}}, status=400)),
        )
        with pytest.raises(ProviderUnavailableError, match="400"):
            await provider.generate(ASK)

    async def test_a_rejected_key_says_so_without_echoing_it(self) -> None:
        provider = OpenAILLMProvider(
            api_key="secret-key", client=openai_client(responder({}, status=401))
        )
        with pytest.raises(ProviderUnavailableError) as caught:
            await provider.generate(ASK)
        assert "OPENAI_API_KEY" in str(caught.value)
        assert "secret-key" not in str(caught.value)

    async def test_a_transient_failure_is_retried_once(self) -> None:
        attempts = {"n": 0}

        def handle(_request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return httpx.Response(503, json={})
            return httpx.Response(200, json=COMPLETION)

        provider = OpenAILLMProvider(api_key="k", client=openai_client(handle))
        assert (await provider.generate(ASK)).text == "We are open 8 to 5."
        assert attempts["n"] == 2

    async def test_a_client_error_is_not_retried(self) -> None:
        """Retrying a 400 spends money twice for the same answer."""
        attempts = {"n": 0}

        def handle(_request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            return httpx.Response(400, json={"error": {"message": "nope"}})

        provider = OpenAILLMProvider(api_key="k", client=openai_client(handle))
        with pytest.raises(ProviderUnavailableError):
            await provider.generate(ASK)
        assert attempts["n"] == 1

    async def test_an_empty_choice_list_is_an_error_not_an_empty_answer(self) -> None:
        provider = OpenAILLMProvider(api_key="k", client=openai_client(responder({"choices": []})))
        with pytest.raises(ProviderUnavailableError, match="no choices"):
            await provider.generate(ASK)

    async def test_a_key_is_required(self) -> None:
        with pytest.raises(ValueError, match="API key"):
            OpenAILLMProvider(api_key="")


TOOLS = [
    ToolSpec(
        name="get_patient_appointments",
        description="Upcoming appointments",
        parameters={"type": "object", "properties": {"patient_id": {"type": "string"}}},
    )
]


def tool_response(arguments: str, name: str = "get_patient_appointments") -> dict[str, Any]:
    return {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {"id": "call-1", "function": {"name": name, "arguments": arguments}}
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 200, "completion_tokens": 20},
    }


class TestOpenAIToolCalls:
    async def test_a_proposed_call_is_returned_with_parsed_arguments(self) -> None:
        provider = OpenAILLMProvider(
            api_key="k",
            client=openai_client(responder(tool_response('{"patient_id": "demo-john-smith"}'))),
        )
        response = await provider.tool_call(ToolCallRequest(messages=ASK.messages, tools=TOOLS))
        assert len(response.tool_calls) == 1
        call = response.tool_calls[0]
        assert call.name == "get_patient_appointments"
        assert call.arguments == {"patient_id": "demo-john-smith"}

    async def test_unparseable_arguments_are_dropped_not_guessed_at(self) -> None:
        """A call repaired by the adapter is a call nobody reviewed."""
        provider = OpenAILLMProvider(
            api_key="k", client=openai_client(responder(tool_response("{not json")))
        )
        response = await provider.tool_call(ToolCallRequest(messages=ASK.messages, tools=TOOLS))
        assert response.tool_calls == []

    async def test_the_tool_schema_is_sent_in_openai_shape(self) -> None:
        seen: dict[str, Any] = {}

        def handle(request: httpx.Request) -> httpx.Response:
            seen.update(json.loads(request.content))
            return httpx.Response(200, json=tool_response("{}"))

        provider = OpenAILLMProvider(api_key="k", client=openai_client(handle))
        await provider.tool_call(ToolCallRequest(messages=ASK.messages, tools=TOOLS))
        assert seen["tools"][0]["type"] == "function"
        assert seen["tools"][0]["function"]["name"] == "get_patient_appointments"
        assert seen["tool_choice"] == "auto"


class FakeSocket:
    """The two methods the adapter uses, and nothing else."""

    def __init__(self, messages: list[str]) -> None:
        self.queued = list(messages)
        self.sent: list[bytes] = []

    async def send(self, data: bytes) -> None:
        self.sent.append(data)

    async def recv(self, timeout: float | None = None) -> str | None:
        if not self.queued:
            if timeout == 0:
                raise TimeoutError
            return None
        return self.queued.pop(0)

    async def __aenter__(self) -> FakeSocket:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


def deepgram_message(text: str, is_final: bool, duration: float = 1.5) -> str:
    return json.dumps(
        {
            "type": "Results",
            "is_final": is_final,
            "duration": duration,
            "channel": {"alternatives": [{"transcript": text, "confidence": 0.97}]},
        }
    )


async def one_chunk() -> AsyncIterator[bytes]:
    yield b"\x00" * 640


class TestDeepgram:
    def _connect(self, socket: FakeSocket) -> Any:
        def connect(_url: str, **_kwargs: Any) -> FakeSocket:
            return socket

        return connect

    async def test_final_transcripts_are_yielded(self) -> None:
        socket = FakeSocket([deepgram_message("I need an appointment", is_final=True)])
        provider = DeepgramSTTProvider(api_key="k", connect=self._connect(socket))

        results = [t async for t in provider.transcribe_stream(one_chunk())]
        assert [t.text for t in results] == ["I need an appointment"]
        assert results[0].is_final is True

    async def test_audio_seconds_are_metered_for_final_results(self) -> None:
        ledger = UsageLedger()
        socket = FakeSocket([deepgram_message("hello", is_final=True, duration=2.0)])
        provider = DeepgramSTTProvider(
            api_key="k", ledger=ledger, session_id="sess-1", connect=self._connect(socket)
        )

        [t async for t in provider.transcribe_stream(one_chunk())]
        assert ledger.session_quantity("sess-1", "stt_seconds") == Decimal("2.0")
        assert ledger.project_total() > Decimal("0")

    async def test_interim_results_are_not_metered(self) -> None:
        """Interim text is re-sent as it is refined; billing it would double-count."""
        ledger = UsageLedger()
        socket = FakeSocket([deepgram_message("hel", is_final=False, duration=2.0)])
        provider = DeepgramSTTProvider(
            api_key="k", ledger=ledger, session_id="sess-1", connect=self._connect(socket)
        )

        results = [t async for t in provider.transcribe_stream(one_chunk())]
        assert results[0].is_final is False
        assert ledger.project_total() == Decimal("0")

    async def test_empty_and_unparseable_frames_are_ignored(self) -> None:
        socket = FakeSocket(["not json", json.dumps({"type": "Metadata"})])
        provider = DeepgramSTTProvider(api_key="k", connect=self._connect(socket))
        assert [t async for t in provider.transcribe_stream(one_chunk())] == []

    async def test_the_endpoint_requests_the_configured_model(self) -> None:
        provider = DeepgramSTTProvider(api_key="k", model="nova-3")
        assert "model=nova-3" in provider.endpoint
        assert "interim_results=true" in provider.endpoint

    async def test_a_socket_failure_becomes_a_typed_provider_error(self) -> None:
        def connect(_url: str, **_kwargs: Any) -> Any:
            raise OSError("connection refused")

        provider = DeepgramSTTProvider(api_key="k", connect=connect)
        with pytest.raises(ProviderUnavailableError, match="Deepgram"):
            [t async for t in provider.transcribe_stream(one_chunk())]

    async def test_a_key_is_required(self) -> None:
        with pytest.raises(ValueError, match="API key"):
            DeepgramSTTProvider(api_key="")


class TestElevenLabs:
    def _client(self, handler: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.elevenlabs.test/v1"
        )

    def _audio(self, status: int = 200) -> Any:
        def handle(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, content=b"\x01\x02\x03\x04")

        return handle

    async def test_audio_is_streamed_back(self) -> None:
        provider = ElevenLabsTTSProvider(
            api_key="k", voice_id="v", client=self._client(self._audio())
        )
        chunks = [c async for c in provider.synthesize_stream("Hello there", VoiceSpec())]
        assert b"".join(chunks) == b"\x01\x02\x03\x04"

    async def test_characters_are_metered(self) -> None:
        ledger = UsageLedger()
        provider = ElevenLabsTTSProvider(
            api_key="k",
            voice_id="v",
            ledger=ledger,
            session_id="sess-1",
            client=self._client(self._audio()),
        )
        text = "You're booked in for Tuesday."
        [c async for c in provider.synthesize_stream(text, VoiceSpec())]
        assert ledger.session_quantity("sess-1", TTS_CHARACTERS) == Decimal(str(len(text)))
        assert ledger.project_total() > Decimal("0")

    async def test_empty_text_costs_nothing_and_calls_nothing(self) -> None:
        ledger = UsageLedger()

        def handle(_request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("no request should be made for empty text")

        provider = ElevenLabsTTSProvider(
            api_key="k", voice_id="v", ledger=ledger, client=self._client(handle)
        )
        assert [c async for c in provider.synthesize_stream("   ", VoiceSpec())] == []
        assert ledger.project_total() == Decimal("0")

    async def test_an_error_becomes_a_typed_provider_error(self) -> None:
        provider = ElevenLabsTTSProvider(
            api_key="k", voice_id="v", client=self._client(self._audio(status=422))
        )
        with pytest.raises(ProviderUnavailableError, match="422"):
            [c async for c in provider.synthesize_stream("hello", VoiceSpec())]

    async def test_the_voice_spec_overrides_the_default_voice(self) -> None:
        seen: dict[str, Any] = {}

        def handle(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, content=b"\x00")

        provider = ElevenLabsTTSProvider(
            api_key="k", voice_id="default-voice", client=self._client(handle)
        )
        [c async for c in provider.synthesize_stream("hi", VoiceSpec(voice_id="other-voice"))]
        assert "other-voice" in seen["path"]

    async def test_a_key_and_voice_are_required(self) -> None:
        with pytest.raises(ValueError, match="API key"):
            ElevenLabsTTSProvider(api_key="", voice_id="v")
        with pytest.raises(ValueError, match="voice id"):
            ElevenLabsTTSProvider(api_key="k", voice_id="")


class TestGroqCompatibility:
    """Groq speaks the OpenAI wire format, with one documented difference.

    It rejects ``messages[].name``, and the only message that carries one is a
    tool-repair message. So an unfiltered payload works perfectly until the
    first time a model gets its arguments wrong -- which is both the worst
    moment to find out and the hardest case to reach by accident.
    """

    def _provider(self, handler: Any, **kwargs: Any) -> OpenAILLMProvider:
        return OpenAILLMProvider(
            api_key="gsk-test",
            model="llama-3.3-70b-versatile",
            client=openai_client(handler),
            name="groq",
            supports_message_name=False,
            **kwargs,
        )

    def _capture(self, sink: dict[str, Any]) -> Any:
        def handle(request: httpx.Request) -> httpx.Response:
            sink.update(json.loads(request.content))
            return httpx.Response(200, json=COMPLETION)

        return handle

    async def test_the_unsupported_name_field_is_stripped(self) -> None:
        sent: dict[str, Any] = {}
        provider = self._provider(self._capture(sent))
        await provider.generate(
            LLMRequest(
                messages=[
                    ChatMessage(
                        role="tool",
                        tool_call_id="call-1",
                        name="book_appointment",
                        content="INVALID_ARGUMENTS: slot_id: Field required",
                    )
                ]
            )
        )
        assert "name" not in sent["messages"][0]
        assert sent["messages"][0]["tool_call_id"] == "call-1"
        assert sent["messages"][0]["content"].startswith("INVALID_ARGUMENTS")

    async def test_openai_keeps_the_name_field(self) -> None:
        sent: dict[str, Any] = {}
        provider = OpenAILLMProvider(api_key="k", client=openai_client(self._capture(sent)))
        await provider.generate(
            LLMRequest(
                messages=[
                    ChatMessage(role="tool", tool_call_id="c", name="book_appointment", content="x")
                ]
            )
        )
        assert sent["messages"][0]["name"] == "book_appointment"

    async def test_a_repair_message_survives_the_round_trip(self) -> None:
        """The end-to-end version of the above, through the real rejection type."""
        from app.ai.tool_calls import ToolCallRejection

        rejection = ToolCallRejection(
            tool_call_id="call-1",
            name="book_appointment",
            code="INVALID_ARGUMENTS",
            message="slot_id: Field required",
        )
        sent: dict[str, Any] = {}
        provider = self._provider(self._capture(sent))
        await provider.generate(LLMRequest(messages=[rejection.as_repair_message()]))
        assert "name" not in sent["messages"][0]

    async def test_usage_is_recorded_against_groq_at_zero_cost(self) -> None:
        ledger = UsageLedger()
        provider = self._provider(responder(COMPLETION), ledger=ledger)
        await provider.generate(ASK)

        assert {r.provider for r in ledger.records} == {"groq"}
        assert sum(r.quantity for r in ledger.records if r.metric == INPUT_TOKENS) == 120
        # Free tier: metered but not priced.
        assert ledger.project_total() == Decimal("0")

    async def test_a_rate_limit_is_reported_as_such(self) -> None:
        provider = self._provider(
            responder({"error": {"message": "rate limit reached"}}, status=429)
        )
        with pytest.raises(ProviderUnavailableError, match="rate limit"):
            await provider.generate(ASK)

    async def test_reasoning_effort_is_sent_when_configured(self) -> None:
        """gpt-oss models think before answering, on the output token budget.

        Verified live: without this, a 16-token cap returned an empty message
        that had spent all 16 tokens reasoning.
        """
        sent: dict[str, Any] = {}
        provider = self._provider(self._capture(sent), reasoning_effort="low")
        await provider.generate(ASK)
        assert sent["reasoning_effort"] == "low"

    async def test_reasoning_effort_is_absent_for_a_model_without_it(self) -> None:
        """Sending it to a non-reasoning model is a 400, not a no-op."""
        sent: dict[str, Any] = {}
        provider = OpenAILLMProvider(api_key="k", client=openai_client(self._capture(sent)))
        await provider.generate(ASK)
        assert "reasoning_effort" not in sent

    async def test_errors_name_the_provider_that_failed(self) -> None:
        provider = self._provider(responder({}, status=401))
        with pytest.raises(ProviderUnavailableError) as caught:
            await provider.generate(ASK)
        assert "GROQ_API_KEY" in str(caught.value)
        assert "gsk-test" not in str(caught.value)
