"""Browser voice transport.

A plain WebSocket, not WebRTC. The thing barge-in actually needs is acoustic
echo cancellation -- without it the agent hears itself and interrupts itself --
and AEC is supplied by the *browser*, via ``getUserMedia({echoCancellation})``,
whatever carries the bytes afterwards. WebRTC would add jitter buffering and
packet-loss concealment, which matter over a lossy network and not at all
between a tab and a server on the same machine; a hosted SFU would add a round
trip to somebody else's datacentre to a latency budget measured in hundreds of
milliseconds (docs/voice-architecture.md, ADR 007).

Wire protocol, deliberately small:

* client -> server: binary frames of 16 kHz mono 16-bit PCM; JSON ``{"type":
  "hangup"}`` to end the call.
* server -> client: binary frames of 24 kHz mono 16-bit PCM; JSON events for
  ``ready``, ``transcript``, ``interrupt`` and ``closed``.

``interrupt`` is the one that is easy to leave out and impossible to fake.
Cancelling synthesis server-side stops us *producing* audio, but whatever
already crossed the socket is sitting in the browser's playback queue -- so the
agent would keep talking for another second or two after being interrupted.
The client drops its queue on this event.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.agents.factory import Runtime
from app.agents.state import SessionChannel
from app.ai.budget_guard import BudgetGuard
from app.ai.providers.base import STTProvider, Transcript
from app.ai.providers.factory import build_stt_provider, build_tts_provider
from app.ai.providers.stt.deepgram import DEFAULT_SAMPLE_RATE as INPUT_SAMPLE_RATE
from app.ai.providers.tts.groq import SAMPLE_RATE as OUTPUT_SAMPLE_RATE
from app.ai.usage import get_usage_ledger
from app.api.agent import get_runtime
from app.config.settings import get_settings
from app.observability.logging import get_logger
from app.voice.models import CloseReason
from app.voice.session import VoiceSession

logger = get_logger(__name__)

router = APIRouter(tags=["voice"])

#: How much undelivered microphone audio to hold before dropping frames.
#: Audio that is seconds late is not worth transcribing -- the caller has moved
#: on -- so a bounded queue that discards is more honest than an unbounded one
#: that buffers a growing delay and calls it working.
AUDIO_QUEUE_FRAMES = 100

#: How far ahead of the caller's ear the transport is allowed to get.
#:
#: Synthesis runs about six times faster than speech, so an unpaced transport
#: pushes a whole utterance to the browser in a fraction of its duration. That
#: overruns the client's playback buffer -- which sounds like crackling -- and
#: leaves the server believing the agent finished talking long before it did.
#: Sending at roughly the rate it is heard fixes both, and costs nothing: the
#: caller cannot listen faster than real time either way.
#:
#: The lead absorbs network jitter. Too small and playback stutters; too large
#: and it is the unpaced case again.
PLAYOUT_LEAD_SECONDS = 1.5


@router.websocket("/ws/voice")
async def voice_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    settings = get_settings()

    if settings.text_only_mode or not (settings.stt_enabled and settings.tts_enabled):
        # Refused with a reason rather than dropped: the default configuration
        # is text-only, so this is the expected answer for most checkouts and
        # the message is the only thing that says how to change it.
        await _send_json(
            websocket,
            None,
            {
                "type": "error",
                "detail": (
                    "Voice is disabled. Set TEXT_ONLY_MODE=false with STT_ENABLED=true "
                    "and TTS_ENABLED=true to enable it (docs/voice-architecture.md)."
                ),
            },
        )
        await websocket.close(code=1000)
        return

    runtime: Runtime = get_runtime()
    session = runtime.sessions.create(channel=SessionChannel.VOICE)
    ledger = get_usage_ledger()

    stt = build_stt_provider(settings, ledger, session.session_id)
    tts = build_tts_provider(settings, ledger, session.session_id)
    if stt is None or tts is None:  # pragma: no cover - guarded above
        await websocket.close(code=1011)
        return

    # One lock over the socket. Audio chunks and JSON events are produced by
    # different tasks, and two interleaved `send`s is a corrupted frame.
    send_lock = asyncio.Lock()
    audio: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=AUDIO_QUEUE_FRAMES)

    playout = _Playout(OUTPUT_SAMPLE_RATE)

    async def audio_out(chunk: bytes) -> None:
        await playout.pace(chunk)
        async with send_lock:
            if websocket.client_state is WebSocketState.CONNECTED:
                await websocket.send_bytes(chunk)

    async def drain() -> None:
        await playout.drain()

    async def on_interrupt() -> None:
        # Reset before telling the client, so the next utterance starts its
        # own clock rather than inheriting the cursor of the one abandoned.
        playout.reset()
        await _send_json(websocket, send_lock, {"type": "interrupt"})

    async def on_close(reason: CloseReason) -> None:
        await _send_json(
            websocket,
            send_lock,
            {"type": "closed", "reason": reason.value, "session_id": session.session_id},
        )

    async def relay(transcript: Transcript) -> None:
        await _send_json(
            websocket,
            send_lock,
            {
                "type": "transcript",
                "text": transcript.text,
                "is_final": transcript.is_final,
            },
        )

    voice = VoiceSession(
        session=session,
        orchestrator=runtime.orchestrator,
        stt=_TranscriptRelay(stt, relay),
        tts=tts,
        audio_out=audio_out,
        guard=BudgetGuard(settings, ledger),
        on_close=on_close,
        on_interrupt=on_interrupt,
        drain=drain,
        timings_sink=_timings_sink(runtime),
    )

    await _send_json(
        websocket,
        send_lock,
        {
            "type": "ready",
            "session_id": session.session_id,
            "input_sample_rate": INPUT_SAMPLE_RATE,
            "output_sample_rate": OUTPUT_SAMPLE_RATE,
            "stt": stt.name,
            "tts": tts.name,
        },
    )
    logger.info("voice_socket_open", session_id=session.session_id, stt=stt.name, tts=tts.name)

    reader = asyncio.create_task(_read_client(websocket, audio, session.session_id))
    try:
        await voice.run(_drain(audio))
    except Exception as exc:
        logger.error("voice_socket_failed", session_id=session.session_id, error=str(exc))
        await _send_json(websocket, send_lock, {"type": "error", "detail": str(exc)})
    finally:
        reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reader
        # Ending the session is what writes it to the database, so it happens
        # however the call ended -- including the failure path above. A call
        # that vanished from the dashboard because the socket broke would be
        # exactly the call worth looking at.
        with contextlib.suppress(Exception):
            await runtime.end_session(session.session_id)
        if websocket.client_state is WebSocketState.CONNECTED:
            await websocket.close(code=1000)
        logger.info(
            "voice_socket_closed",
            session_id=session.session_id,
            turns=voice.manager.stats.turns,
            barge_ins=voice.manager.stats.barge_ins,
        )


async def _read_client(
    websocket: WebSocket, audio: asyncio.Queue[bytes | None], session_id: str
) -> None:
    """Feed microphone frames into the queue until the client goes away."""
    dropped = 0
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            chunk = message.get("bytes")
            if chunk:
                try:
                    audio.put_nowait(chunk)
                except asyncio.QueueFull:
                    # Backpressure we cannot apply upstream: a microphone does
                    # not pause. Dropping the newest frame keeps the delay
                    # bounded, and the count says it happened.
                    dropped += 1
                continue
            text = message.get("text")
            if text and _is_hangup(text):
                break
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if dropped:
            logger.warning("voice_audio_frames_dropped", session_id=session_id, dropped=dropped)
        # The sentinel is what ends the transcription stream, which is what
        # ends the call. Without it a disconnected client leaves a session
        # ticking until its ten-minute limit.
        await audio.put(None)


async def _drain(audio: asyncio.Queue[bytes | None]) -> AsyncIterator[bytes]:
    while True:
        chunk = await audio.get()
        if chunk is None:
            return
        yield chunk


def _timings_sink(
    runtime: Any,
) -> Callable[[str, float | None, float | None], Awaitable[None]] | None:
    """Route the voice stage latencies to persistence, if it is switched on.

    Returns ``None`` when it is not, so a run without a database is not paying
    to discover that on every turn.
    """
    persistence = getattr(runtime, "persistence", None)
    if persistence is None:
        return None

    async def sink(turn_id: str, stt_ms: float | None, tts_first_audio_ms: float | None) -> None:
        await persistence.record_voice_timings(
            turn_id, stt_ms=stt_ms, tts_first_audio_ms=tts_first_audio_ms
        )

    return sink


def _is_hangup(text: str) -> bool:
    try:
        return bool(json.loads(text).get("type") == "hangup")
    except (TypeError, ValueError, AttributeError):
        return False


async def _send_json(
    websocket: WebSocket, lock: asyncio.Lock | None, payload: dict[str, Any]
) -> None:
    """Send an event, tolerating a socket that has already gone.

    Every one of these fires during teardown, when the client disconnecting is
    the normal case rather than an error.
    """
    if websocket.client_state is not WebSocketState.CONNECTED:
        return
    try:
        if lock is None:
            await websocket.send_json(payload)
        else:
            async with lock:
                await websocket.send_json(payload)
    except (WebSocketDisconnect, RuntimeError):
        pass


class _TranscriptRelay:
    """Wraps an STT provider to copy transcripts to the client.

    A decorator rather than a hook on ``VoiceSession``: the session's job is to
    be transport-agnostic, and "show the caller what we heard" is a property of
    having a screen, which SIP (Phase 15) will not have.
    """

    def __init__(self, inner: STTProvider, relay: Any) -> None:
        self._inner = inner
        self._relay = relay
        self.name = inner.name

    def transcribe_stream(self, audio: AsyncIterator[bytes]) -> AsyncIterator[Transcript]:
        return self._stream(audio)

    async def _stream(self, audio: AsyncIterator[bytes]) -> AsyncIterator[Transcript]:
        async for transcript in self._inner.transcribe_stream(audio):
            await self._relay(transcript)
            yield transcript


class _Playout:
    """Paces outgoing audio to the rate it is actually heard.

    Keeps a cursor at the moment the audio sent so far will finish playing.
    Sending is allowed to run ahead of that by ``PLAYOUT_LEAD_SECONDS`` and no
    further, which bounds both the client's buffer and the error in the
    server's belief about when the agent stopped talking.

    Deliberately not a wall-clock sleep of the whole utterance: chunks must
    keep flowing so that playback starts immediately and a barge-in can cut in
    part-way through.
    """

    def __init__(self, sample_rate: int) -> None:
        self.sample_rate = sample_rate
        self._plays_until: float | None = None

    def _now(self) -> float:
        return asyncio.get_running_loop().time()

    def reset(self) -> None:
        self._plays_until = None

    async def pace(self, chunk: bytes) -> None:
        seconds = len(chunk) / (self.sample_rate * 2)
        now = self._now()
        if self._plays_until is None or self._plays_until < now:
            # First chunk of an utterance, or playback has caught up with us.
            self._plays_until = now
        ahead = self._plays_until - now
        if ahead > PLAYOUT_LEAD_SECONDS:
            await asyncio.sleep(ahead - PLAYOUT_LEAD_SECONDS)
        self._plays_until += seconds

    async def drain(self) -> None:
        """Wait out the audio still in the client's buffer."""
        if self._plays_until is None:
            return
        remaining = self._plays_until - self._now()
        self._plays_until = None
        if remaining > 0:
            await asyncio.sleep(remaining)
