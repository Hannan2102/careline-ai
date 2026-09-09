"""ElevenLabs streaming text-to-speech.

Streamed rather than fetched whole: what matters to a caller is *time to first
audio*, not total synthesis time, and the two differ by seconds on a long
sentence (docs/latency.md).

Billed per character, so the meter counts the characters actually sent -- not
the characters the workflow intended to send.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx

from app.ai.providers.base import ProviderUnavailableError, VoiceSpec
from app.ai.usage import TTS_CHARACTERS, UsageLedger
from app.observability.logging import get_logger

logger = get_logger(__name__)

DEFAULT_BASE_URL = "https://api.elevenlabs.io/v1"

#: Telephony-shaped by default: 8 kHz µ-law is what a SIP leg wants, and the
#: browser path resamples happily from it (docs/voice-architecture.md).
DEFAULT_OUTPUT_FORMAT = "ulaw_8000"


class ElevenLabsTTSProvider:
    """``TTSProvider`` backed by the ElevenLabs streaming endpoint."""

    name = "elevenlabs"

    def __init__(
        self,
        api_key: str,
        voice_id: str,
        model: str = "eleven_flash_v2_5",
        base_url: str = DEFAULT_BASE_URL,
        output_format: str = DEFAULT_OUTPUT_FORMAT,
        timeout: float = 30.0,
        ledger: UsageLedger | None = None,
        session_id: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("ElevenLabsTTSProvider requires an API key")
        if not voice_id:
            raise ValueError("ElevenLabsTTSProvider requires a voice id")
        self.voice_id = voice_id
        self.model = model
        self.output_format = output_format
        self.ledger = ledger
        self.session_id = session_id
        # On the request rather than the client, for the same reason as the
        # OpenAI adapter: an injected client must not silently lose the key.
        self._headers = {"xi-api-key": api_key, "Content-Type": "application/json"}
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def synthesize_stream(self, text: str, voice: VoiceSpec) -> AsyncIterator[bytes]:
        spoken = text.strip()
        if not spoken:
            return

        voice_id = voice.voice_id or self.voice_id
        payload = {
            "text": spoken,
            "model_id": voice.model or self.model,
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75, "speed": voice.speed},
        }
        request = self._client.build_request(
            "POST",
            f"/text-to-speech/{voice_id}/stream",
            json=payload,
            params={"output_format": self.output_format},
            headers=self._headers,
        )

        # Metered before the first byte arrives: the vendor bills for the
        # characters submitted, so a stream the caller abandons still costs.
        self._meter(spoken)

        try:
            response = await self._client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(f"ElevenLabs request failed: {exc}") from exc

        try:
            if response.status_code >= 400:
                await response.aread()
                raise ProviderUnavailableError(self._error_message(response))
            async for chunk in response.aiter_bytes():
                if chunk:
                    yield chunk
        finally:
            await response.aclose()

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        if response.status_code == 401:
            return "ElevenLabs rejected the API key (401). Check ELEVENLABS_API_KEY."
        return f"ElevenLabs returned {response.status_code}"

    def _meter(self, text: str) -> None:
        if self.ledger is not None:
            self.ledger.record(self.name, TTS_CHARACTERS, len(text), self.session_id)
