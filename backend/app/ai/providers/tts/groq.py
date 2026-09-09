"""Groq streaming text-to-speech (Canopy Labs Orpheus).

Chosen over ElevenLabs after measuring both: Orpheus reaches first audio in
~400 ms against Flash's ~75-150 ms, but costs nothing on Groq's free tier. The
extra ~300 ms sits inside the per-turn latency budget (docs/latency.md), and
the money it saves is the whole project budget (COSTS.md).

Two things about this endpoint are load-bearing:

* **WAV is the only output format.** ``pcm``, ``mp3`` and ``opus`` are all
  rejected, so the adapter strips the container itself and yields raw PCM.
* **The header is unbounded.** The stream opens ``RIFF\\xff\\xff\\xff\\xffWAVE``
  because the total length is not known when the first byte is sent. Anything
  that trusts the declared frame count reads it as a 24-hour file -- so nothing
  here reads it. The body *is* the length.
"""

from __future__ import annotations

import struct
from collections.abc import AsyncIterator

import httpx

from app.ai.providers.base import ProviderUnavailableError, VoiceSpec
from app.ai.usage import TTS_CHARACTERS, UsageLedger
from app.observability.logging import get_logger

logger = get_logger(__name__)

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "canopylabs/orpheus-v1-english"

#: Orpheus emits 24 kHz mono 16-bit PCM. Declared here because the transport
#: has to tell the browser what it is receiving -- headerless PCM carries no
#: sample rate of its own.
SAMPLE_RATE = 24000
SAMPLE_WIDTH = 2
CHANNELS = 1

#: The voices the model accepts. Sending anything else is a 400, so an unknown
#: request-scoped voice falls back to the configured one rather than failing a
#: call over a typo.
VOICES: frozenset[str] = frozenset({"autumn", "diana", "hannah", "austin", "daniel", "troy"})
DEFAULT_VOICE = "hannah"


class GroqTTSProvider:
    """``TTSProvider`` backed by Groq's ``/audio/speech`` endpoint."""

    name = "groq"

    #: What ``synthesize_stream`` yields, for a transport that must describe it.
    sample_rate = SAMPLE_RATE
    channels = CHANNELS

    def __init__(
        self,
        api_key: str,
        voice: str = DEFAULT_VOICE,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        ledger: UsageLedger | None = None,
        session_id: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("GroqTTSProvider requires an API key")
        self.voice = voice if voice in VOICES else DEFAULT_VOICE
        self.model = model
        self.ledger = ledger
        self.session_id = session_id
        # Per request, not on the client: an injected client must not silently
        # lose the key (docs/provider-abstraction.md).
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def synthesize_stream(self, text: str, voice: VoiceSpec) -> AsyncIterator[bytes]:
        spoken = text.strip()
        if not spoken:
            return

        requested = voice.voice_id or self.voice
        payload = {
            "model": voice.model or self.model,
            "voice": requested if requested in VOICES else self.voice,
            "input": spoken,
            "response_format": "wav",
        }
        request = self._client.build_request(
            "POST", "/audio/speech", json=payload, headers=self._headers
        )

        # Metered before the first byte, as with every other provider: the
        # vendor counts what was submitted, so an abandoned stream still spent
        # its quota. Priced at zero here -- see FREE_PROVIDERS in ai/usage.py.
        self._meter(spoken)

        try:
            response = await self._client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(f"Groq TTS request failed: {exc}") from exc

        try:
            if response.status_code >= 400:
                await response.aread()
                raise ProviderUnavailableError(self._error_message(response))
            stripper = _WavHeaderStripper()
            async for chunk in response.aiter_bytes():
                pcm = stripper.feed(chunk)
                if pcm:
                    yield pcm
        finally:
            await response.aclose()

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        if response.status_code == 401:
            return "Groq rejected the API key (401). Check GROQ_API_KEY."
        detail = ""
        try:
            detail = str((response.json().get("error") or {}).get("message") or "")
        except ValueError:
            detail = response.text[:200]
        if "terms" in detail.lower():
            return (
                "Groq needs the model's terms accepted once, by an org admin, at "
                "https://console.groq.com/playground?model=canopylabs/orpheus-v1-english"
            )
        return (
            f"Groq TTS returned {response.status_code}: {detail}"
            if detail
            else (f"Groq TTS returned {response.status_code}")
        )

    def _meter(self, text: str) -> None:
        if self.ledger is not None:
            self.ledger.record(self.name, TTS_CHARACTERS, len(text), self.session_id)


class _WavHeaderStripper:
    """Turns a streamed RIFF/WAVE body into bare PCM frames.

    Written as a small state machine rather than "drop the first 44 bytes"
    because a WAV header is not a fixed size: the spec allows any number of
    chunks before ``data``, and a vendor adding a ``LIST`` chunk would shift
    every sample by a few bytes -- which is inaudible as an error and sounds
    exactly like static.

    Chunk boundaries fall wherever the network puts them, so the header may
    arrive split across reads. Bytes are held until a full chunk header is
    readable rather than assumed to be present.
    """

    _HEADER = 8  # a chunk id plus its declared size

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._in_data = False

    def feed(self, chunk: bytes) -> bytes:
        if self._in_data:
            return chunk
        self._buffer.extend(chunk)
        return self._consume_header()

    def _consume_header(self) -> bytes:
        # RIFF<size>WAVE, then chunks until `data`.
        if len(self._buffer) < 12:
            return b""
        if bytes(self._buffer[:4]) != b"RIFF" or bytes(self._buffer[8:12]) != b"WAVE":
            # Not a container at all: pass it through untouched rather than
            # discarding audio on an assumption about the vendor's framing.
            self._in_data = True
            payload = bytes(self._buffer)
            self._buffer.clear()
            return payload

        offset = 12
        while True:
            if len(self._buffer) < offset + self._HEADER:
                # Nothing is consumed until `data` is found: the buffer is
                # re-parsed from the top on the next read. Headers are tens of
                # bytes, so re-parsing is free -- whereas advancing a cursor
                # that a partial read can invalidate is how this silently
                # starts decoding audio from the middle of a chunk header.
                return b""
            chunk_id = bytes(self._buffer[offset : offset + 4])
            (size,) = struct.unpack("<I", self._buffer[offset + 4 : offset + self._HEADER])
            offset += self._HEADER
            if chunk_id == b"data":
                self._in_data = True
                payload = bytes(self._buffer[offset:])
                self._buffer.clear()
                return payload
            # Non-data chunks are padded to even length by the spec.
            offset += size + (size & 1)
