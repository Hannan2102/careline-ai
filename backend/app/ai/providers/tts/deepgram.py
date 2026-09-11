"""Deepgram text-to-speech (Aura), over a websocket by default.

Chosen because the project already buys Deepgram for recognition, and the same
key speaks. That matters more than it sounds: Groq's free tier meters Orpheus
at 3,600 tokens *per day*, which is roughly one full test call, and a project
that cannot be exercised twice in an evening cannot be debugged. Deepgram bills
per character against credit already held, so the cap stops being a factor.

Two paths to the same audio, and the difference is a connection rather than
the synthesis. REST opens one per utterance -- a caller speaks for several
seconds between replies, and httpx expires an idle connection after five -- so
almost every reply pays for a TLS handshake before a single sample is made.
The websocket is opened once and held for the call.

Measured interleaved, with seven seconds between utterances as a real call
has: REST **445 ms** to first audio, REST with a two-minute keepalive
**335 ms**, this socket **130 ms** (docs/latency.md). Back to back with no gap
all three are identical at ~125 ms, which is why the first version of that
benchmark showed no improvement at all and nearly buried the change.

REST remains reachable with ``streaming=False``, and is fallen back to when
the socket will not open: a websocket has more ways to fail than a POST, and
the alternative in the guarded wrapper is the mock, which is silence.

Unlike Groq, this endpoint will emit bare PCM on request -- ``container=none``
-- so there is usually no header to remove. The stripper runs anyway: it passes
non-RIFF bytes through untouched, and costs nothing to be wrong about.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing
from typing import Any
from urllib.parse import urlencode

import httpx

from app.ai.providers.base import ProviderUnavailableError, VoiceSpec
from app.ai.providers.tts.groq import _WavHeaderStripper
from app.ai.usage import TTS_CHARACTERS, UsageLedger
from app.observability.logging import get_logger

logger = get_logger(__name__)

DEFAULT_BASE_URL = "https://api.deepgram.com"
DEFAULT_SOCKET_URL = "wss://api.deepgram.com/v1/speak"

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
        streaming: bool = True,
        socket_url: str = DEFAULT_SOCKET_URL,
        connect: Any | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("DeepgramTTSProvider requires an API key")
        self.model = model
        self.ledger = ledger
        self.session_id = session_id
        self.streaming = streaming
        self._api_key = api_key
        self._socket_url = socket_url
        #: Injected in tests so the adapter's own logic is exercised without a
        #: socket. Production passes nothing and the real client is imported.
        self._connect = connect
        #: Held open for the life of the call. See ``_over_socket``.
        self._socket: Any | None = None
        self._socket_params: dict[str, str] | None = None
        # Per request, not on the client: an injected client must not silently
        # lose the key (docs/provider-abstraction.md).
        self._headers = {"Authorization": f"Token {api_key}", "Content-Type": "application/json"}
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)

    async def aclose(self) -> None:
        await self._discard_socket()
        if self._owns_client:
            await self._client.aclose()

    async def synthesize_stream(self, text: str, voice: VoiceSpec) -> AsyncIterator[bytes]:
        spoken = text.strip()
        if not spoken:
            return

        if not self.streaming:
            async for chunk in self._over_rest(spoken, voice):
                yield chunk
            return

        # A socket has more ways to fail than a POST, and REST works. Falling
        # back costs a second submission, which is metered, and is still far
        # better than the guarded wrapper's only other option -- the mock,
        # which is silence.
        heard_anything = False
        try:
            # `aclosing`, because closing *this* generator is what barge-in
            # does and an `async for` does not close the generator it is
            # iterating. Without it the socket's own cleanup waits for garbage
            # collection, and the interrupted utterance is still queued on it
            # when the next reply reuses the connection.
            async with aclosing(self._over_socket(spoken, voice)) as audio:
                async for chunk in audio:
                    heard_anything = True
                    yield chunk
            return
        except Exception as exc:
            if heard_anything:
                # Half an utterance has already been spoken. Starting again
                # from the beginning would say the first half twice, which is
                # worse than the silence that ends it.
                raise ProviderUnavailableError(f"Deepgram speech socket failed: {exc}") from exc
            logger.warning("deepgram_tts_socket_failed_falling_back", error=str(exc))

        async for chunk in self._over_rest(spoken, voice):
            yield chunk

    def _params(self, voice: VoiceSpec) -> dict[str, str]:
        """Query for either path. The voice *is* the model here.

        A request-scoped voice therefore selects a model, which is why an
        unknown one is passed through rather than validated against a list:
        Deepgram adds voices, and a stale allow-list in this file would reject
        a voice that works.
        """
        return {
            "model": voice.voice_id or voice.model or self.model,
            "encoding": "linear16",
            "sample_rate": str(SAMPLE_RATE),
            "container": "none",
        }

    async def _over_socket(self, spoken: str, voice: VoiceSpec) -> AsyncGenerator[bytes]:
        """Audio as it is produced, rather than once it is finished.

        The whole point of the phase: the REST endpoint synthesises the entire
        utterance before answering, so the first byte arrives after the last
        one is made. Here the first chunk arrives while the rest is still being
        made.

        ``Flush`` is what makes that true. Without it Deepgram waits for more
        text, because the socket is designed for a model streaming words in as
        it writes them; we have the whole sentence already and want it spoken
        now.

        The socket is **kept open for the call**, which is the difference
        between this being an optimisation and a regression. Opening a fresh
        one per utterance measured *slower* than REST: a handshake and an
        upgrade cost more than streaming saves. Held open, every reply after
        the first skips both.
        """
        socket = await self._ensure_socket(voice)
        self._meter(spoken)
        finished = False
        try:
            await socket.send(json.dumps({"type": "Speak", "text": spoken}))
            await socket.send(json.dumps({"type": "Flush"}))
            stripper = _WavHeaderStripper()
            async for message in socket:
                if isinstance(message, bytes):
                    pcm = stripper.feed(message)
                    if pcm:
                        yield pcm
                    continue
                if self._is_finished(message):
                    finished = True
                    break
        finally:
            # Anything other than a clean finish leaves audio we did not read
            # queued on the socket -- which is exactly what barge-in does, and
            # reusing it would play the interrupted sentence into the next one.
            if not finished:
                await self._discard_socket()

    async def _ensure_socket(self, voice: VoiceSpec) -> Any:
        """The call's socket, opened on first use and reused after that.

        Reopened when the voice changes, because the voice is part of the
        connection's query string rather than of a message.
        """
        params = self._params(voice)
        if self._socket is not None and self._socket_params == params:
            return self._socket

        await self._discard_socket()
        connect = self._connect or self._default_connect()
        endpoint = f"{self._socket_url}?{urlencode(params)}"
        self._socket = await connect(
            endpoint, additional_headers={"Authorization": f"Token {self._api_key}"}
        )
        self._socket_params = params
        return self._socket

    async def _discard_socket(self) -> None:
        """Close and forget, never failing the utterance over it."""
        socket, self._socket, self._socket_params = self._socket, None, None
        if socket is None:
            return
        try:
            await socket.send(json.dumps({"type": "Close"}))
        except Exception as exc:  # pragma: no cover - a socket already gone
            logger.debug("deepgram_tts_close_message_failed", error=str(exc))
        try:
            await socket.close()
        except Exception as exc:  # pragma: no cover - a socket already gone
            logger.debug("deepgram_tts_close_failed", error=str(exc))

    @staticmethod
    def _is_finished(message: str) -> bool:
        """Whether a text frame means the utterance is complete.

        ``Flushed`` is the one that matters. An error arrives the same way and
        must not be mistaken for an ending, or a failed synthesis looks like a
        successful silent one.
        """
        try:
            kind = str(json.loads(message).get("type", ""))
        except (TypeError, ValueError):
            return False
        if kind in {"Error", "Warning"}:
            raise ProviderUnavailableError(f"Deepgram speech socket said: {message[:200]}")
        return kind in {"Flushed", "Close"}

    def _default_connect(self) -> Any:
        """Imported lazily, so only a Deepgram user needs ``websockets``."""
        try:
            from websockets.asyncio.client import connect
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise ProviderUnavailableError(
                "Deepgram speech streaming needs the 'websockets' package."
            ) from exc
        return connect

    async def _over_rest(self, spoken: str, voice: VoiceSpec) -> AsyncIterator[bytes]:
        params = self._params(voice)
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
