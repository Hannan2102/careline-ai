"""Deepgram Aura text-to-speech.

Added because Groq's free tier meters speech at 3,600 tokens per *day* --
roughly one full test call -- and a voice project that cannot be exercised
twice in an evening cannot be debugged. Deepgram bills per character against
credit the project already holds for recognition, and the same key speaks.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from decimal import Decimal

import httpx
import pytest

from app.ai.providers.base import ProviderUnavailableError, VoiceSpec
from app.ai.providers.tts.deepgram import DEFAULT_MODEL, SAMPLE_RATE, DeepgramTTSProvider
from app.ai.usage import TTS_CHARACTERS, UsageLedger, price

PCM = b"\x01\x02\x03\x04" * 16

Handler = Callable[[httpx.Request], httpx.Response]


def provider(handler: Handler, **kwargs: object) -> DeepgramTTSProvider:
    client = httpx.AsyncClient(
        base_url="https://api.deepgram.com", transport=httpx.MockTransport(handler)
    )
    return DeepgramTTSProvider(api_key="k", client=client, **kwargs)  # type: ignore[arg-type]


def recording(sink: dict) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        sink["url"] = str(request.url)
        sink["params"] = dict(request.url.params)
        sink["body"] = json.loads(request.content)
        sink["auth"] = request.headers.get("Authorization", "")
        return httpx.Response(200, content=PCM)

    return handler


async def collect(tts: DeepgramTTSProvider, text: str, voice: VoiceSpec | None = None) -> bytes:
    return b"".join([c async for c in tts.synthesize_stream(text, voice or VoiceSpec())])


class TestSynthesis:
    @pytest.mark.asyncio
    async def test_streams_the_audio_through(self) -> None:
        assert await collect(provider(lambda _: httpx.Response(200, content=PCM)), "Hi.") == PCM

    @pytest.mark.asyncio
    async def test_asks_for_the_format_the_transport_already_speaks(self) -> None:
        """24 kHz linear16 with no container.

        The same shape Groq is made to produce, so the browser needs no
        per-provider branch and the sample rate it is told about stays true.
        """
        seen: dict = {}
        await collect(provider(recording(seen)), "Hello.")
        assert seen["params"]["encoding"] == "linear16"
        assert seen["params"]["sample_rate"] == str(SAMPLE_RATE)
        assert seen["params"]["container"] == "none"

    @pytest.mark.asyncio
    async def test_sends_the_text_and_a_token_credential(self) -> None:
        """Deepgram uses `Token`, not `Bearer`. A Bearer header is a 401."""
        seen: dict = {}
        await collect(provider(recording(seen)), "Hello.")
        assert seen["body"] == {"text": "Hello."}
        assert seen["auth"] == "Token k"

    @pytest.mark.asyncio
    async def test_the_voice_selects_the_model(self) -> None:
        """Aura has no separate voice field: the voice *is* the model."""
        seen: dict = {}
        await collect(provider(recording(seen)), "Hi.", VoiceSpec(voice_id="aura-2-orion-en"))
        assert seen["params"]["model"] == "aura-2-orion-en"

    @pytest.mark.asyncio
    async def test_an_unknown_voice_is_passed_through_not_rejected(self) -> None:
        """Deliberately no allow-list.

        Deepgram adds voices, and a stale set hard-coded here would refuse one
        that works -- a worse failure than the 400 it was trying to avoid.
        """
        seen: dict = {}
        await collect(provider(recording(seen)), "Hi.", VoiceSpec(voice_id="aura-brand-new-en"))
        assert seen["params"]["model"] == "aura-brand-new-en"

    @pytest.mark.asyncio
    async def test_the_configured_model_is_the_default(self) -> None:
        seen: dict = {}
        await collect(provider(recording(seen)), "Hi.")
        assert seen["params"]["model"] == DEFAULT_MODEL

    @pytest.mark.asyncio
    async def test_empty_text_makes_no_request(self) -> None:
        def explode(_: httpx.Request) -> httpx.Response:
            raise AssertionError("synthesised nothing at all")

        assert await collect(provider(explode), "   ") == b""


class TestFailures:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (401, "Check DEEPGRAM_API_KEY"),
            (402, "no remaining credit"),
            (429, "429"),
        ],
    )
    async def test_errors_say_what_to_do(self, status: int, expected: str) -> None:
        """The 429 that ended an evening said everything needed, in the log.

        These messages are the only thing standing between a vendor refusal and
        an hour spent looking for a bug in this repository.
        """
        tts = provider(lambda _: httpx.Response(status, json={"err_msg": "nope"}))
        with pytest.raises(ProviderUnavailableError) as caught:
            await collect(tts, "Hi.")
        assert expected in str(caught.value)

    @pytest.mark.asyncio
    async def test_a_transport_failure_is_a_provider_failure(self) -> None:
        """So the guard can fall back rather than the call raising."""

        def refuse(_: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host")

        with pytest.raises(ProviderUnavailableError):
            await collect(provider(refuse), "Hi.")


class TestMetering:
    @pytest.mark.asyncio
    async def test_characters_are_recorded_against_the_session(self) -> None:
        ledger = UsageLedger()
        tts = provider(
            lambda _: httpx.Response(200, content=PCM), ledger=ledger, session_id="sess-1"
        )
        await collect(tts, "Hello there.")
        record = ledger.records[-1]
        assert record.provider == "deepgram"
        assert record.metric == TTS_CHARACTERS
        assert record.quantity == len("Hello there.")
        assert record.session_id == "sess-1"

    def test_speech_is_priced_rather_than_free(self) -> None:
        """An unpriced pair costs zero, and a free provider cannot be capped.

        Deepgram is metered, so a missing rate would not merely misreport -- it
        would disable the budget guard for the one provider that spends money
        on every single turn.
        """
        assert price("deepgram", TTS_CHARACTERS, 1000) > Decimal("0")
