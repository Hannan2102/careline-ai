"""Groq (Orpheus) text-to-speech adapter.

The header stripper gets most of the attention because it is the part that
fails *inaudibly*: a few bytes of offset does not raise, it turns speech into
static, and no assertion anywhere else in the suite would notice.
"""

from __future__ import annotations

import json
import struct
from collections.abc import Callable

import httpx
import pytest

from app.ai.providers.base import ProviderUnavailableError, VoiceSpec
from app.ai.providers.tts.groq import DEFAULT_VOICE, GroqTTSProvider, _WavHeaderStripper
from app.ai.usage import TTS_CHARACTERS, UsageLedger


def wav(pcm: bytes, extra_chunks: bytes = b"") -> bytes:
    """A RIFF/WAVE container around ``pcm``, framed the way Groq frames it.

    The size field is 0xFFFFFFFF because the total length is not known when
    streaming starts -- which is why nothing in the adapter reads it.
    """
    fmt = struct.pack("<4sIHHIIHH", b"fmt ", 16, 1, 1, 24000, 48000, 2, 16)
    body = fmt + extra_chunks + b"data" + struct.pack("<I", len(pcm)) + pcm
    return b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVE" + body


PCM = bytes(range(256)) * 4


class TestHeaderStripper:
    def test_yields_pcm_without_the_container(self) -> None:
        assert _WavHeaderStripper().feed(wav(PCM)) == PCM

    def test_survives_a_header_split_across_reads(self) -> None:
        """The network decides where reads break, not the vendor.

        A stripper that advances a cursor on a partial read resumes from the
        wrong offset and shifts every sample -- the failure that sounds like
        static rather than raising.
        """
        stream = wav(PCM)
        for split in (1, 4, 11, 12, 20, 40, 43, 44, 50):
            stripper = _WavHeaderStripper()
            out = stripper.feed(stream[:split]) + stripper.feed(stream[split:])
            assert out == PCM, f"lost sync when the read broke at byte {split}"

    def test_survives_one_byte_at_a_time(self) -> None:
        stripper = _WavHeaderStripper()
        assert b"".join(stripper.feed(bytes([b])) for b in wav(PCM)) == PCM

    def test_skips_chunks_before_data(self) -> None:
        """A `LIST` chunk appearing one day must not shift the audio."""
        listing = b"LIST" + struct.pack("<I", 10) + b"INFOhello\x00"
        assert _WavHeaderStripper().feed(wav(PCM, extra_chunks=listing)) == PCM

    def test_skips_an_odd_length_chunk_with_its_pad_byte(self) -> None:
        odd = b"note" + struct.pack("<I", 3) + b"abc\x00"
        assert _WavHeaderStripper().feed(wav(PCM, extra_chunks=odd)) == PCM

    def test_passes_through_a_body_that_is_not_a_container(self) -> None:
        """Better to play raw bytes than to discard audio on an assumption."""
        assert _WavHeaderStripper().feed(PCM) == PCM

    def test_does_not_re_scan_once_inside_the_data_chunk(self) -> None:
        """Audio can contain the bytes `RIFF`; that must not restart parsing."""
        stripper = _WavHeaderStripper()
        stripper.feed(wav(PCM))
        assert stripper.feed(b"RIFF\xff\xff\xff\xffWAVE") == b"RIFF\xff\xff\xff\xffWAVE"


Handler = Callable[[httpx.Request], httpx.Response]


def provider(handler: Handler, **kwargs: object) -> GroqTTSProvider:
    client = httpx.AsyncClient(
        base_url="https://api.groq.com/openai/v1", transport=httpx.MockTransport(handler)
    )
    return GroqTTSProvider(api_key="k", client=client, **kwargs)  # type: ignore[arg-type]


def recording(sink: dict) -> Handler:
    """A handler that captures the request and returns one utterance."""

    def handler(request: httpx.Request) -> httpx.Response:
        sink["body"] = json.loads(request.content)
        sink["auth"] = request.headers.get("Authorization", "")
        return httpx.Response(200, content=wav(PCM))

    return handler


async def collect(tts: GroqTTSProvider, text: str, voice: VoiceSpec | None = None) -> bytes:
    return b"".join([c async for c in tts.synthesize_stream(text, voice or VoiceSpec())])


class TestSynthesis:
    @pytest.mark.asyncio
    async def test_streams_bare_pcm(self) -> None:
        tts = provider(lambda _: httpx.Response(200, content=wav(PCM)))
        assert await collect(tts, "Hi.") == PCM

    @pytest.mark.asyncio
    async def test_requests_the_only_format_the_endpoint_accepts(self) -> None:
        """`pcm`, `mp3`, `opus`, `flac` and `ogg` are all 400s -- measured."""
        seen: dict = {}
        await collect(provider(recording(seen)), "Hello.")
        assert seen["body"]["response_format"] == "wav"

    @pytest.mark.asyncio
    async def test_an_unknown_voice_falls_back_rather_than_failing_the_call(self) -> None:
        """An unlisted voice is a 400. A typo in config should not end a call."""
        seen: dict = {}
        tts = provider(recording(seen), voice="nonexistent")
        await collect(tts, "Hi.", VoiceSpec(voice_id="also-bogus"))
        assert seen["body"]["voice"] == DEFAULT_VOICE

    @pytest.mark.asyncio
    async def test_empty_text_makes_no_request(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("synthesised nothing but still called the vendor")

        assert await collect(provider(handler), "   ") == b""

    @pytest.mark.asyncio
    async def test_credentials_go_on_the_request_not_the_client(self) -> None:
        """An injected client must not silently lose the key."""
        seen: dict = {}
        await collect(provider(recording(seen)), "Hi.")
        assert seen["auth"] == "Bearer k"

    @pytest.mark.asyncio
    async def test_meters_the_characters_submitted(self) -> None:
        ledger = UsageLedger()
        await collect(provider(recording({}), ledger=ledger), " Hello there. ")
        record = ledger.records[0]
        assert (record.provider, record.metric, int(record.quantity)) == (
            "groq",
            TTS_CHARACTERS,
            12,
        )

    @pytest.mark.asyncio
    async def test_free_tier_usage_is_priced_at_zero(self) -> None:
        ledger = UsageLedger()
        await collect(provider(recording({}), ledger=ledger), "Hello.")
        assert ledger.project_total() == 0

    @pytest.mark.asyncio
    async def test_missing_terms_acceptance_says_how_to_fix_it(self) -> None:
        """The genuine first response from this endpoint. The URL is the answer."""
        body = {"error": {"message": "The model requires terms acceptance."}}
        with pytest.raises(ProviderUnavailableError, match=r"console\.groq\.com"):
            await collect(provider(lambda _: httpx.Response(400, json=body)), "Hi.")

    @pytest.mark.asyncio
    async def test_a_rejected_key_is_named_as_such(self) -> None:
        with pytest.raises(ProviderUnavailableError, match=r"GROQ_API_KEY"):
            await collect(provider(lambda _: httpx.Response(401, json={})), "Hi.")

    @pytest.mark.asyncio
    async def test_a_transport_failure_becomes_a_provider_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host")

        with pytest.raises(ProviderUnavailableError):
            await collect(provider(handler), "Hi.")
