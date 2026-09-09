"""Deepgram streaming speech-to-text.

Streaming, not batch: the latency budget (docs/latency.md) assumes recognition
finalises while the caller is still speaking, and a batch transcription of a
finished utterance cannot do that.

``websockets`` is imported lazily so the dependency is only needed by someone
who actually configures Deepgram. The default install, CI, and every test stay
free of it.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlencode

from app.ai.providers.base import ProviderUnavailableError, Transcript
from app.ai.usage import STT_SECONDS, UsageLedger
from app.observability.logging import get_logger

logger = get_logger(__name__)

DEFAULT_URL = "wss://api.deepgram.com/v1/listen"

#: 16 kHz mono 16-bit PCM: what the browser and telephony paths both produce
#: after resampling (docs/voice-architecture.md).
DEFAULT_ENCODING = "linear16"
DEFAULT_SAMPLE_RATE = 16000


class DeepgramSTTProvider:
    """``STTProvider`` backed by Deepgram's streaming endpoint."""

    name = "deepgram"

    def __init__(
        self,
        api_key: str,
        model: str = "nova-3",
        url: str = DEFAULT_URL,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        ledger: UsageLedger | None = None,
        session_id: str | None = None,
        connect: Any | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("DeepgramSTTProvider requires an API key")
        self._api_key = api_key
        self.model = model
        self.sample_rate = sample_rate
        self.ledger = ledger
        self.session_id = session_id
        #: Injected in tests so the adapter's own logic is exercised without a
        #: socket. Production passes nothing and the real client is imported.
        self._connect = connect

    @property
    def endpoint(self) -> str:
        query = urlencode(
            {
                "model": self.model,
                "encoding": DEFAULT_ENCODING,
                "sample_rate": self.sample_rate,
                "channels": 1,
                "interim_results": "true",
                "punctuate": "true",
                # Deepgram decides where an utterance ends; the orchestrator
                # only ever acts on a final transcript.
                "endpointing": "300",
            }
        )
        return f"{DEFAULT_URL}?{query}" if "?" not in DEFAULT_URL else f"{DEFAULT_URL}&{query}"

    async def transcribe_stream(self, audio: AsyncIterator[bytes]) -> AsyncIterator[Transcript]:
        connect = self._connect or self._default_connect()
        try:
            async with connect(
                self.endpoint, additional_headers={"Authorization": f"Token {self._api_key}"}
            ) as socket:
                async for chunk in audio:
                    await socket.send(chunk)
                    async for transcript in self._drain(socket):
                        yield transcript
                # Deepgram flushes and closes on an empty binary frame.
                await socket.send(b"")
                async for transcript in self._drain(socket, until_closed=True):
                    yield transcript
        except ProviderUnavailableError:
            raise
        except Exception as exc:  # vendor and socket errors alike
            raise ProviderUnavailableError(f"Deepgram stream failed: {exc}") from exc

    async def _drain(self, socket: Any, until_closed: bool = False) -> AsyncIterator[Transcript]:
        """Yield whatever the socket has ready.

        Non-blocking while audio is still arriving: waiting for a response
        between chunks would serialise the stream and spend the latency budget
        on nothing.
        """
        while True:
            message = await (socket.recv() if until_closed else self._recv_nowait(socket))
            if message is None:
                return
            transcript = self._parse(message)
            if transcript is None:
                continue
            if transcript.is_final and self.ledger is not None and transcript.audio_seconds:
                self.ledger.record(
                    self.name, STT_SECONDS, transcript.audio_seconds, self.session_id
                )
            yield transcript

    @staticmethod
    async def _recv_nowait(socket: Any) -> Any:
        """One message if one is waiting, otherwise ``None``."""
        try:
            return await socket.recv(timeout=0)
        except TimeoutError:
            return None

    @staticmethod
    def _parse(message: Any) -> Transcript | None:
        if isinstance(message, bytes):
            return None
        try:
            payload: dict[str, Any] = json.loads(message)
        except (TypeError, ValueError):
            return None
        if payload.get("type") not in (None, "Results"):
            return None

        alternatives = ((payload.get("channel") or {}).get("alternatives")) or []
        if not alternatives:
            return None
        best = alternatives[0]
        text = str(best.get("transcript") or "")
        if not text:
            return None
        return Transcript(
            text=text,
            is_final=bool(payload.get("is_final")),
            confidence=best.get("confidence"),
            audio_seconds=float(payload.get("duration") or 0.0),
        )

    @staticmethod
    def _default_connect() -> Any:
        try:
            from websockets.asyncio.client import connect
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise ProviderUnavailableError(
                "Deepgram needs the 'websockets' package: pip install '.[cloud]'"
            ) from exc
        return connect
