"""Telephony transport: a real phone call, over Twilio Media Streams.

The same shape as the browser transport in ``voice_ws.py`` and, deliberately,
the same ``VoiceSession`` behind it. Nothing about business logic, safety,
verification or wording knows which of the two a caller arrived through --
that is the claim ADR 005 makes, and this is the second transport that has to
prove it.

Three things are genuinely different, and all three make telephony *easier*
than the browser was:

*The format is the carrier's.* 8 kHz mu-law both ways, asked of Deepgram and
Aura directly rather than converted here (voice/telephony.py). No resampling,
no companding, no header stripping.

*Barge-in is a primitive.* The browser needed a bespoke `interrupt` event
because audio already sent is queued in the page. Twilio has `clear`.

*Playback completion is reported.* The browser transport estimates when the
caller has finished hearing a reply by pacing the bytes; Twilio echoes a
`mark` back when it has actually played to that point, which is the same
question answered by the party that knows.

What telephony adds is that anyone can dial it. The webhook that starts these
calls is signature-checked (``api/twilio_voice.py``); this socket is not, and
cannot be -- Twilio does not sign the stream. It is protected instead by the
stream URL being unguessable per call and by the session limits every call
already has: ten minutes, twenty turns, and a spending cap.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.agents.factory import Runtime
from app.agents.state import SessionChannel
from app.ai.budget_guard import BudgetGuard
from app.ai.providers.factory import build_stt_provider, build_tts_provider
from app.ai.usage import get_usage_ledger
from app.api.agent import get_runtime
from app.config.settings import get_settings
from app.observability.logging import get_logger
from app.voice import telephony
from app.voice.session import VoiceSession

logger = get_logger(__name__)

router = APIRouter(tags=["voice"])

#: How long to wait for Twilio to confirm it has played an utterance before
#: giving up on the mark and letting the conversation continue.
#:
#: A safety net, not a timing mechanism. If a mark is lost the call must not
#: stall in "still speaking" forever -- the agent would never hear the caller
#: again. Generous, because the alternative failure is talking over somebody.
MARK_TIMEOUT_SECONDS = 30.0

AUDIO_QUEUE_FRAMES = 200


@router.websocket("/twilio/media")
async def twilio_media(websocket: WebSocket) -> None:
    await websocket.accept()
    settings = get_settings()

    if settings.text_only_mode or not (settings.stt_enabled and settings.tts_enabled):
        logger.warning("twilio_stream_refused_voice_disabled")
        await websocket.close(code=1000)
        return

    runtime: Runtime = get_runtime()
    session = runtime.sessions.create(channel=SessionChannel.VOICE)
    ledger = get_usage_ledger()

    # The carrier's format, asked of the vendors rather than converted here.
    stt = build_stt_provider(
        settings,
        ledger,
        session.session_id,
        sample_rate=telephony.SAMPLE_RATE,
        encoding=telephony.ENCODING,
    )
    tts = build_tts_provider(
        settings,
        ledger,
        session.session_id,
        sample_rate=telephony.SAMPLE_RATE,
        encoding=telephony.ENCODING,
    )
    if stt is None or tts is None:  # pragma: no cover - guarded above
        await websocket.close(code=1011)
        return

    stream = _Stream(websocket)
    audio: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=AUDIO_QUEUE_FRAMES)

    voice = VoiceSession(
        session=session,
        orchestrator=runtime.orchestrator,
        stt=stt,
        tts=tts,
        audio_out=stream.play,
        guard=BudgetGuard(settings, ledger),
        on_interrupt=stream.stop_playing,
        drain=stream.wait_until_heard,
        timings_sink=_timings_sink(runtime),
    )

    reader = asyncio.create_task(_read_twilio(websocket, stream, audio, session.session_id))
    try:
        await voice.run(_drain(audio))
    except Exception as exc:
        logger.error("twilio_stream_failed", session_id=session.session_id, error=str(exc))
    finally:
        reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reader
        with contextlib.suppress(Exception):
            await runtime.end_session(session.session_id)
        if websocket.client_state is WebSocketState.CONNECTED:
            await websocket.close(code=1000)
        logger.info(
            "twilio_stream_closed",
            session_id=session.session_id,
            call_sid=stream.call_sid,
            turns=voice.manager.stats.turns,
            barge_ins=voice.manager.stats.barge_ins,
        )


class _Stream:
    """The outbound half: audio, barge-in, and knowing when it was heard."""

    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket
        self._lock = asyncio.Lock()
        self.stream_sid: str | None = None
        self.call_sid: str | None = None
        self._marks = 0
        self._played: asyncio.Event | None = None
        self._awaiting_mark: str | None = None

    async def play(self, audio: bytes) -> None:
        if self.stream_sid is None or not audio:
            return
        await self._send(telephony.media_frame(self.stream_sid, audio))

    async def stop_playing(self) -> None:
        """Barge-in: drop whatever Twilio has buffered but not yet played."""
        if self.stream_sid is None:
            return
        # Released first. The utterance being cleared is one nobody will hear
        # the end of, so a `drain` still waiting on its mark would wait out
        # the timeout with the caller already talking.
        self._release_mark()
        await self._send(telephony.clear_frame(self.stream_sid))

    async def wait_until_heard(self) -> None:
        """Block until Twilio says it has played everything sent so far.

        Twilio accepts audio far faster than it plays it, so returning when
        the last byte was written would tell the session the agent had stopped
        talking seconds early -- and it would start the silence timer over its
        own voice, which is the bug that made the browser transport ask "are
        you still there?" mid-sentence.
        """
        if self.stream_sid is None:
            return
        self._marks += 1
        name = f"utterance-{self._marks}"
        self._awaiting_mark = name
        played = asyncio.Event()
        self._played = played
        await self._send(telephony.mark_frame(self.stream_sid, name))
        try:
            await asyncio.wait_for(played.wait(), timeout=MARK_TIMEOUT_SECONDS)
        except TimeoutError:
            logger.warning("twilio_mark_never_returned", mark=name)
        finally:
            self._awaiting_mark = None
            self._played = None

    def mark_played(self, name: str) -> None:
        if name == self._awaiting_mark:
            self._release_mark()

    def _release_mark(self) -> None:
        if self._played is not None:
            self._played.set()

    async def _send(self, frame: str) -> None:
        async with self._lock:
            if self._websocket.client_state is WebSocketState.CONNECTED:
                with contextlib.suppress(WebSocketDisconnect, RuntimeError):
                    await self._websocket.send_text(frame)


async def _read_twilio(
    websocket: WebSocket,
    stream: _Stream,
    audio: asyncio.Queue[bytes | None],
    session_id: str,
) -> None:
    """Inbound frames: the caller's audio, and the stream's own lifecycle."""
    dropped = 0
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            text = message.get("text")
            if not text:
                continue

            event, payload = telephony.parse(text)
            match event:
                case "media" if isinstance(payload, bytes):
                    try:
                        audio.put_nowait(payload)
                    except asyncio.QueueFull:
                        # A microphone does not pause. Dropping the newest
                        # frame keeps the delay bounded; the count says so.
                        dropped += 1
                case "start" if isinstance(payload, telephony.StreamStarted):
                    stream.stream_sid = payload.stream_sid
                    stream.call_sid = payload.call_sid
                    logger.info(
                        "twilio_stream_started",
                        session_id=session_id,
                        call_sid=payload.call_sid,
                    )
                case "mark" if isinstance(payload, str):
                    stream.mark_played(payload)
                case "stop":
                    break
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if dropped:
            logger.warning("twilio_frames_dropped", session_id=session_id, dropped=dropped)
        # Ends the recognition stream, which is what ends the call. Without it
        # a hung-up caller leaves a session ticking to its ten-minute limit.
        await audio.put(None)


async def _drain(audio: asyncio.Queue[bytes | None]) -> AsyncIterator[bytes]:
    while True:
        chunk = await audio.get()
        if chunk is None:
            return
        yield chunk


def _timings_sink(runtime: Runtime):  # type: ignore[no-untyped-def]
    persistence = getattr(runtime, "persistence", None)
    if persistence is None:
        return None

    async def sink(turn_id: str, stt_ms: float | None, tts_first_audio_ms: float | None) -> None:
        await persistence.record_voice_timings(
            turn_id, stt_ms=stt_ms, tts_first_audio_ms=tts_first_audio_ms
        )

    return sink
