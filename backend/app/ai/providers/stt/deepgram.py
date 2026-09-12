"""Deepgram streaming speech-to-text.

Streaming, not batch: the latency budget (docs/latency.md) assumes recognition
finalises while the caller is still speaking, and a batch transcription of a
finished utterance cannot do that.

``websockets`` is imported lazily so the dependency is only needed by someone
who actually configures Deepgram. The default install, CI, and every test stay
free of it.
"""

from __future__ import annotations

import asyncio
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
        #: "mulaw" for a phone call. Asking the recogniser for the format the
        #: carrier already speaks is cheaper and more faithful than companding
        #: to PCM in Python on the way past (voice/telephony.py).
        encoding: str = DEFAULT_ENCODING,
        ledger: UsageLedger | None = None,
        session_id: str | None = None,
        connect: Any | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("DeepgramSTTProvider requires an API key")
        self._api_key = api_key
        self.model = model
        self.sample_rate = sample_rate
        self.encoding = encoding
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
                "encoding": self.encoding,
                "sample_rate": self.sample_rate,
                "channels": 1,
                "interim_results": "true",
                "punctuate": "true",
                # `smart_format` is deliberately NOT enabled, despite being the
                # obvious choice for dates. It rewrites "the fourth of March
                # nineteen seventy eight" as "03/04/1978" -- US order, and
                # indistinguishable from 3 April to any parser that has to
                # guess. A third of dates are ambiguous that way, in the one
                # place where guessing wrong means failing to identify a real
                # patient. It also mangles a bare year: "nineteen seventy
                # eight" came back as "19 70 8" in testing.
                #
                # The spoken form is unambiguous, so it is kept and resolved
                # deterministically in `agents/extraction.py` instead.
                # Deepgram decides where an utterance ends; the orchestrator
                # only ever acts on a final transcript.
                "endpointing": "300",
            }
        )
        return f"{DEFAULT_URL}?{query}" if "?" not in DEFAULT_URL else f"{DEFAULT_URL}&{query}"

    async def transcribe_stream(self, audio: AsyncIterator[bytes]) -> AsyncIterator[Transcript]:
        """Stream audio up and transcripts down, concurrently.

        Sending and receiving run as separate tasks because they are genuinely
        independent: Deepgram returns interim results *while* the caller is
        still speaking, which is the entire reason for choosing streaming
        recognition over batch (COSTS.md). Polling for a reply between sends
        would serialise them and spend the latency budget on nothing.

        An earlier version did exactly that, using a non-blocking `recv` --
        against an API that has no such parameter. It failed on the first real
        connection, having passed its tests, because the test double had been
        written to match the mistake.
        """
        connect = self._connect or self._default_connect()
        try:
            async with connect(
                self.endpoint, additional_headers={"Authorization": f"Token {self._api_key}"}
            ) as socket:
                pump = asyncio.create_task(self._pump(socket, audio))
                try:
                    # Deepgram closes the socket once it has finalised
                    # everything it was sent, which is what ends this loop.
                    async for message in socket:
                        transcript = self._parse(message)
                        if transcript is None:
                            continue
                        if (
                            transcript.is_final
                            and self.ledger is not None
                            and transcript.audio_seconds
                        ):
                            self.ledger.record(
                                self.name, STT_SECONDS, transcript.audio_seconds, self.session_id
                            )
                        yield transcript
                finally:
                    await self._stop(pump)
        except ProviderUnavailableError:
            raise
        except Exception as exc:  # vendor and socket errors alike
            raise ProviderUnavailableError(f"Deepgram stream failed: {exc}") from exc

    async def _pump(self, socket: Any, audio: AsyncIterator[bytes]) -> None:
        """Forward microphone audio, then ask Deepgram to finalise."""
        async for chunk in audio:
            await socket.send(chunk)
        # Tells Deepgram to flush its buffer, emit any last final, and close.
        # Without it a caller who stops talking waits for a timeout instead of
        # hearing an answer.
        await socket.send(json.dumps({"type": "CloseStream"}))

    @staticmethod
    async def _stop(pump: asyncio.Task[None]) -> None:
        """Stop the sender, surfacing a genuine failure but not a cancellation."""
        current = asyncio.current_task()
        pump.cancel()
        try:
            await pump
        except asyncio.CancelledError:
            # Ours, not this task's -- unless this task is itself being
            # cancelled, which is a different thing and must propagate.
            # `CancelledError` is a BaseException, so `suppress(Exception)`
            # would not catch it here either.
            if current is not None and current.cancelling() > 0:
                raise

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
            # Deepgram's own end-of-speech decision, from its endpointer, and
            # a different thing from `is_final`. `is_final` says this segment
            # is settled; it is raised at every pause, so acting on it answers
            # callers mid-sentence -- observed live as three turns in a row of
            # "my full name is" with the name still to come.
            speech_final=bool(payload.get("speech_final")),
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
