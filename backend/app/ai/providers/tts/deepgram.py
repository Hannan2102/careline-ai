"""Deepgram streaming text-to-speech (Aura).

Chosen because the project already buys Deepgram for recognition, and the same
key speaks. That matters more than it sounds: Groq's free tier meters Orpheus
at 3,600 tokens *per day*, which is roughly one full test call, and a project
that cannot be exercised twice in an evening cannot be debugged. Deepgram bills
per character against credit already held, so the cap stops being a factor.

The latency trade is real and is not yet decided. A cold REST call measured
~490 ms to first byte against Orpheus's warm ~220 ms, which is worse; Deepgram
also offers a streaming websocket that should beat both. Phase 14 measures it
rather than this docstring asserting it (docs/latency.md).

Unlike Groq, this endpoint will emit bare PCM on request -- ``container=none``
-- so there is usually no header to remove. The stripper runs anyway: it passes
non-RIFF bytes through untouched, and costs nothing to be wrong about.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx

from app.ai.providers.base import ProviderUnavailableError, VoiceSpec
from app.ai.providers.tts.groq import _WavHeaderStripper
from app.ai.usage import TTS_CHARACTERS, UsageLedger
from app.observability.logging import get_logger

logger = get_logger(__name__)

DEFAULT_BASE_URL = "https://api.deepgram.com"

#: Aura names the voice *as* the model, so there is no separate voice field.
#: Verified against the live endpoint rather than taken from documentation.
DEFAULT_MODEL = "aura-2-thalia-en"

#: Matches Groq's output, so the transport describes one format whichever
#: provider is configured and the browser needs no per-provider branch.
SAMPLE_RATE = 24000
SAMPLE_WIDTH = 2
CHANNELS = 1


class DeepgramTTSProvider:
    """``TTSProvider`` backed by Deepgram's ``/v1/speak`` endpoint."""

    name = "deepgram"

    #: What ``synthesize_stream`` yields, for a transport that must describe it.
    sample_rate = SAMPLE_RATE
    channels = CHANNELS

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        ledger: UsageLedger | None = None,
        session_id: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("DeepgramTTSProvider requires an API key")
        self.model = model
        self.ledger = ledger
        self.session_id = session_id
        # Per request, not on the client: an injected client must not silently
        # lose the key (docs/provider-abstraction.md).
        self._headers = {"Authorization": f"Token {api_key}", "Content-Type": "application/json"}
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def synthesize_stream(self, text: str, voice: VoiceSpec) -> AsyncIterator[bytes]:
        spoken = text.strip()
        if not spoken:
            return

        # The voice *is* the model here. A request-scoped voice therefore
        # selects a model, which is why an unknown one is passed through rather
        # than validated against a list: Deepgram adds voices, and a stale
        # allow-list in this file would reject a voice that works.
        params = {
            "model": voice.voice_id or voice.model or self.model,
            "encoding": "linear16",
            "sample_rate": str(SAMPLE_RATE),
            "container": "none",
        }
        request = self._client.build_request(
            "POST", "/v1/speak", params=params, json={"text": spoken}, headers=self._headers
        )

        # Metered before the first byte, as with every other provider: the
        # vendor counts what was submitted, so an abandoned stream still spent
        # its quota.
        self._meter(spoken)

        try:
            response = await self._client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(f"Deepgram TTS request failed: {exc}") from exc

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
            return "Deepgram rejected the API key (401). Check DEEPGRAM_API_KEY."
        if response.status_code == 402:
            return (
                "Deepgram reports no remaining credit (402). Check the balance at "
                "https://console.deepgram.com/"
            )
        detail = ""
        try:
            body = response.json()
            detail = str(body.get("err_msg") or body.get("message") or body.get("reason") or "")
        except ValueError:
            detail = response.text[:200]
        return (
            f"Deepgram TTS returned {response.status_code}: {detail}"
            if detail
            else f"Deepgram TTS returned {response.status_code}"
        )

    def _meter(self, text: str) -> None:
        if self.ledger is not None:
            self.ledger.record(self.name, TTS_CHARACTERS, len(text), self.session_id)
