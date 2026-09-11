"""One voice call, end to end.

Binds an STT stream and a TTS provider to the turn manager, and enforces the
per-session spending caps while the call is in progress. Deliberately knows
nothing about *transport*: LiveKit (Phase 13), SIP (Phase 15), or a test
feeding it bytes from a list all look identical from here.

The budget check lives on this path rather than only in the provider factory
because a voice call is the one place spend accumulates while nobody is
watching -- a stuck stream can synthesise for as long as the caller stays on
the line (COSTS.md).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime

from app.agents.orchestrator import Orchestrator
from app.agents.state import SessionState
from app.ai.budget_guard import BudgetGuard
from app.ai.providers.base import STTProvider, TTSProvider, VoiceSpec
from app.observability.logging import get_logger
from app.voice.models import CloseReason, VoiceState
from app.voice.turn_manager import TurnManager, TurnTimings

logger = get_logger(__name__)

#: How often timers are advanced. Fast enough that end-of-utterance detection
#: is not visibly late, slow enough to be free.
TICK_INTERVAL_SECONDS = 0.1

#: What the caller hears when the session's spending cap is reached. It offers
#: a human rather than simply hanging up: the cap is our problem, not theirs.
BUDGET_MESSAGE = (
    "I'm sorry, I need to hand you over to a member of staff. "
    "Please hold and someone will be with you."
)

AudioSink = Callable[[bytes], Awaitable[None]]


class VoiceSession:
    """Runs one call: audio in, audio out, business logic untouched."""

    def __init__(
        self,
        session: SessionState,
        orchestrator: Orchestrator,
        stt: STTProvider,
        tts: TTSProvider,
        audio_out: AudioSink,
        guard: BudgetGuard,
        voice: VoiceSpec | None = None,
        timings: TurnTimings | None = None,
        on_close: Callable[[CloseReason], Awaitable[None]] | None = None,
        on_interrupt: Callable[[], Awaitable[None]] | None = None,
        drain: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.session = session
        self.stt = stt
        self.tts = tts
        self.audio_out = audio_out
        self.guard = guard
        self.voice = voice or VoiceSpec()
        self.budget_exhausted = False
        #: Waits until emitted audio has actually been *heard*.
        #:
        #: Synthesis is far faster than speech: Groq returns 18 seconds of
        #: audio in 3. Without this, ``_speak`` returns when the last byte was
        #: sent rather than when the caller finished hearing it, so the session
        #: believes the agent fell silent fifteen seconds early -- and starts
        #: the silence timer, asking "are you still there?" over its own voice.
        #: A transport that plays in real time supplies this; a test that
        #: counts bytes does not need it.
        self.drain = drain

        self.manager = TurnManager(
            session=session,
            orchestrator=orchestrator,
            speak=self._speak,
            timings=timings,
            on_close=on_close,
            on_interrupt=on_interrupt,
        )

    @property
    def state(self) -> VoiceState:
        return self.manager.state

    async def run(self, audio: AsyncIterator[bytes]) -> None:
        """Drive the call until it closes or the audio stream ends.

        Real time throughout, deliberately. This object *is* the clock for a
        call: it owns the ticker. The turn manager takes injected time so its
        rules can be tested without sleeping; mixing the two here -- starting
        in the past and ticking in the present -- makes every call exceed its
        own time limit on the first tick.
        """
        self.manager.start()
        ticker = asyncio.create_task(self._tick_forever())
        try:
            async for transcript in self.stt.transcribe_stream(audio):
                if self.manager.state is VoiceState.CLOSED:
                    break
                await self.manager.on_transcript(transcript)
        finally:
            ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ticker
            # A stream that has ended is silence by definition, so anything
            # still buffered is a finished utterance. Flushing it means the
            # caller's last words are answered rather than dropped because
            # their audio track happened to end first.
            await self._flush()
            # The stream ending means the caller went away, however it ended.
            await self.manager.close(CloseReason.CALLER_HUNG_UP)

    async def _flush(self) -> None:
        """Run any utterance left buffered when the stream ended."""
        if self.manager.state is VoiceState.CLOSED:
            return
        await self.manager.tick(now=datetime.now(UTC) + self.manager.timings.end_of_utterance)

    async def _tick_forever(self) -> None:
        while self.manager.state is not VoiceState.CLOSED:
            await asyncio.sleep(TICK_INTERVAL_SECONDS)
            await self.manager.tick()

    async def _speak(self, text: str) -> None:
        """Synthesise and emit, unless the session has spent its allowance."""
        decision = self.guard.check(self.tts.name, self.session.session_id)
        if not decision.allowed and not self.budget_exhausted:
            # Say one last thing and stop. Checked before synthesising, so the
            # message that announces the cap does not itself blow through it.
            self.budget_exhausted = True
            logger.warning(
                "voice_budget_exhausted",
                session_id=self.session.session_id,
                reason=decision.reason,
            )
            await self._emit(BUDGET_MESSAGE)
            await self.manager.close(CloseReason.BUDGET)
            return
        if self.budget_exhausted:
            return
        await self._emit(text)

    async def _emit(self, text: str) -> None:
        async for chunk in self.tts.synthesize_stream(text, self.voice):
            await self.audio_out(chunk)
        if self.drain is not None:
            # Cancelled by barge-in along with the rest of the speak task,
            # which is what makes an interruption immediate rather than
            # waiting out audio the caller has already talked over.
            await self.drain()
