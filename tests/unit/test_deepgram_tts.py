"""Deepgram Aura text-to-speech.

Added because Groq's free tier meters speech at 3,600 tokens per *day* --
roughly one full test call -- and a voice project that cannot be exercised
twice in an evening cannot be debugged. Deepgram bills per character against
credit the project already holds for recognition, and the same key speaks.
"""

from __future__ import annotations

import asyncio
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
    """The REST path, and explicitly so.

    ``streaming`` defaults to true in production, and leaving it here would
    make every test below reach for a real websocket, fail to get one, and
    pass anyway down the fallback -- testing the fallback while claiming to
    test the path it falls back to. It did exactly that for one run.
    """
    client = httpx.AsyncClient(
        base_url="https://api.deepgram.com", transport=httpx.MockTransport(handler)
    )
    kwargs.setdefault("streaming", False)  # type: ignore[attr-defined]
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


class FakeSpeakSocket:
    """The surface of a `websockets` connection that the TTS adapter uses.

    Async-iterable like the real one, and it answers a `Flush` the way Deepgram
    does: the audio it was asked for, then a `Flushed` frame. Held open
    afterwards, because that is the property being tested — the adapter keeps
    one socket for the whole call.
    """

    def __init__(self, chunks: list[bytes] | None = None) -> None:
        self.chunks = chunks if chunks is not None else [PCM]
        self.sent: list[str] = []
        self.closed = False
        self._pending: list[bytes | str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)
        kind = json.loads(data).get("type")
        if kind == "Flush":
            self._pending.extend(self.chunks)
            self._pending.append(json.dumps({"type": "Flushed"}))

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self):  # type: ignore[no-untyped-def]
        return self._messages()

    async def _messages(self):  # type: ignore[no-untyped-def]
        while not self.closed:
            if self._pending:
                yield self._pending.pop(0)
            else:
                await asyncio.sleep(0)

    @property
    def spoken(self) -> list[str]:
        return [json.loads(m)["text"] for m in self.sent if json.loads(m)["type"] == "Speak"]


def connecting_to(socket: FakeSpeakSocket, opened: list[str] | None = None):  # type: ignore[no-untyped-def]
    async def connect(url: str, **_kwargs: object) -> FakeSpeakSocket:
        if opened is not None:
            opened.append(url)
        return socket

    return connect


def speaking(socket: FakeSpeakSocket, **kwargs: object) -> DeepgramTTSProvider:
    return DeepgramTTSProvider(
        api_key="k",
        streaming=True,
        connect=connecting_to(socket),
        **kwargs,  # type: ignore[arg-type]
    )


class TestTheWebsocketPath:
    """Phase 14's optimisation: audio as it is made, not once it is finished.

    Measured on `scripts/measure_tts.py`: REST 296 ms median to first audio,
    the socket 115 ms once open. The socket is held for the call, which is what
    makes that true — per-utterance it measured *slower* than REST, because a
    TLS handshake and an upgrade cost more than streaming saves and REST had
    been quietly reusing a warm connection all along.
    """

    @pytest.mark.asyncio
    async def test_it_speaks_and_flushes(self) -> None:
        socket = FakeSpeakSocket()
        audio = await collect(speaking(socket), "Hello there.")

        assert audio == PCM
        assert socket.spoken == ["Hello there."]
        assert [json.loads(m)["type"] for m in socket.sent] == ["Speak", "Flush"], (
            "without the flush, Deepgram waits for more text and the caller hears nothing"
        )

    @pytest.mark.asyncio
    async def test_one_socket_serves_the_whole_call(self) -> None:
        socket, opened = FakeSpeakSocket(), []
        tts = DeepgramTTSProvider(
            api_key="k", streaming=True, connect=connecting_to(socket, opened)
        )

        for line in ("First.", "Second.", "Third."):
            assert await collect(tts, line) == PCM

        assert len(opened) == 1, "reconnected mid-call; the handshake is the cost being avoided"
        assert socket.spoken == ["First.", "Second.", "Third."]

    @pytest.mark.asyncio
    async def test_the_voice_is_in_the_connection(self) -> None:
        socket, opened = FakeSpeakSocket(), []
        tts = DeepgramTTSProvider(
            api_key="k", streaming=True, connect=connecting_to(socket, opened)
        )

        await collect(tts, "Hi.")
        await collect(tts, "Hi.", VoiceSpec(voice_id="aura-2-orion-en"))

        assert len(opened) == 2, "a new voice needs a new connection; it is in the query string"
        assert "aura-2-orion-en" in opened[1]

    @pytest.mark.asyncio
    async def test_an_abandoned_utterance_does_not_leak_into_the_next(self) -> None:
        """What barge-in does, and the hazard a kept-open socket introduces.

        Stopping early leaves audio queued that nobody read. Reusing that
        socket would play the interrupted sentence into the middle of the next
        one, which is worse than the reconnect it costs to avoid.
        """
        socket, opened = FakeSpeakSocket(chunks=[PCM, PCM, PCM]), []
        tts = DeepgramTTSProvider(
            api_key="k", streaming=True, connect=connecting_to(socket, opened)
        )

        stream = tts.synthesize_stream("A long sentence, interrupted.", VoiceSpec())
        await anext(stream)
        await stream.aclose()

        assert socket.closed, "the interrupted socket was kept"
        await collect(tts, "The next thing.")
        assert len(opened) == 2

    @pytest.mark.asyncio
    async def test_an_error_frame_is_not_a_silent_ending(self) -> None:
        """An error arrives the same way a `Flushed` does.

        Treating it as the end turns a failed synthesis into a successful
        silent one, and the caller experiences that as the agent saying
        nothing at all — the failure this project keeps having to design
        against. Here the vendor is out of credit, so REST fails too and the
        real reason surfaces rather than a quiet nothing.
        """

        class Erroring(FakeSpeakSocket):
            async def _messages(self):  # type: ignore[no-untyped-def]
                yield json.dumps({"type": "Error", "description": "no credit"})

        client = httpx.AsyncClient(
            base_url="https://api.deepgram.com",
            transport=httpx.MockTransport(lambda _: httpx.Response(402)),
        )
        tts = DeepgramTTSProvider(
            api_key="k", client=client, streaming=True, connect=connecting_to(Erroring())
        )

        with pytest.raises(ProviderUnavailableError, match="credit"):
            await collect(tts, "Hello.")

    @pytest.mark.asyncio
    async def test_a_socket_that_will_not_open_falls_back_to_rest(self) -> None:
        """REST works. The alternative in the guarded wrapper is silence."""
        calls: dict[str, object] = {}

        async def refuse(_url: str, **_kwargs: object) -> object:
            raise OSError("connection refused")

        client = httpx.AsyncClient(
            base_url="https://api.deepgram.com", transport=httpx.MockTransport(recording(calls))
        )
        tts = DeepgramTTSProvider(api_key="k", client=client, streaming=True, connect=refuse)

        assert await collect(tts, "Hello.") == PCM
        assert calls["body"] == {"text": "Hello."}

    @pytest.mark.asyncio
    async def test_a_socket_that_fails_mid_utterance_does_not_start_again(self) -> None:
        """Half of it has been spoken. Starting over says that half twice."""

        class Failing(FakeSpeakSocket):
            async def _messages(self):  # type: ignore[no-untyped-def]
                yield PCM
                raise OSError("socket died")

        calls: dict[str, object] = {}
        client = httpx.AsyncClient(
            base_url="https://api.deepgram.com", transport=httpx.MockTransport(recording(calls))
        )
        tts = DeepgramTTSProvider(
            api_key="k", client=client, streaming=True, connect=connecting_to(Failing())
        )

        with pytest.raises(ProviderUnavailableError):
            await collect(tts, "Hello.")
        assert "body" not in calls, "said the first half twice"

    @pytest.mark.asyncio
    async def test_it_is_metered_once_per_utterance(self) -> None:
        ledger = UsageLedger()
        tts = speaking(FakeSpeakSocket(), ledger=ledger, session_id="sess-tts")

        await collect(tts, "Hello there.")

        assert ledger.session_quantity("sess-tts", TTS_CHARACTERS) == Decimal("12")
