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
from app.config.clinic import GREETING
from app.observability.logging import get_logger
from app.voice.models import CloseReason, VoiceState
from app.voice.speech import for_speech
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
        timings_sink: Callable[[str, float | None, float | None], Awaitable[None]] | None = None,
        greeting: str | None = GREETING,
    ) -> None:
        self.session = session
        self.stt = stt
        self.tts = tts
        self.audio_out = audio_out
        self.guard = guard
        self.voice = voice or VoiceSpec()
        #: Spoken as soon as the call connects. ``None`` for a call that should
        #: open in silence -- a test asserting on what was synthesised, mostly.
        self.greeting = greeting
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
        #: Where the speech-stage latencies go: ``(turn_id, stt_ms, tts_ms)``.
        #:
        #: A callable rather than the persistence service itself, so this class
        #: stays testable without a database and the numbers can be sent
        #: somewhere else entirely (a metrics sink) without touching it.
        self.timings_sink = timings_sink

        #: Wall-clock of the last audio frame handed to the recogniser, and the
        #: recognition lag derived from it when a final transcript lands.
        #:
        #: This is the honest measure of STT latency: how long after the caller
        #: stopped making sound we knew what they said. It deliberately does
        #: not include our own end-of-utterance wait, which is a policy choice
        #: in TurnTimings rather than anything the recogniser did
        #: (docs/latency.md).
        self._last_audio_at: float | None = None
        self._pending_stt_ms: float | None = None

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
        # A task, not an await: the recogniser's stream is opened by the line
        # below, and greeting first would leave several seconds of the caller's
        # audio queued against a connection nobody had made yet. Someone who
        # answers over the greeting is the normal case, not the exception.
        greeter = asyncio.create_task(self.manager.greet(self.greeting)) if self.greeting else None
        try:
            async for transcript in self.stt.transcribe_stream(self._timed(audio)):
                if self.manager.state is VoiceState.CLOSED:
                    break
                if transcript.is_final and self._last_audio_at is not None:
                    self._pending_stt_ms = (
                        asyncio.get_running_loop().time() - self._last_audio_at
                    ) * 1000
                await self.manager.on_transcript(transcript)
        finally:
            ticker.cancel()
            if greeter is not None:
                greeter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ticker
            if greeter is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await greeter
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

    async def _timed(self, audio: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        """Pass audio through, remembering when the last frame went in."""
        loop = asyncio.get_running_loop()
        async for chunk in audio:
            self._last_audio_at = loop.time()
            yield chunk

    async def _emit(self, text: str) -> None:
        loop = asyncio.get_running_loop()
        started = loop.time()
        first_audio_ms: float | None = None
        # Rewritten here and nowhere else: the transcript and the dashboard
        # keep the written form, which is correct on a screen.
        async for chunk in self.tts.synthesize_stream(for_speech(text), self.voice):
            if first_audio_ms is None:
                # Time to the *first* byte, not the last: what the caller
                # experiences as the gap before the agent starts talking.
                first_audio_ms = (loop.time() - started) * 1000
            await self.audio_out(chunk)
        await self._report_timings(first_audio_ms)
        if self.drain is not None:
            # Cancelled by barge-in along with the rest of the speak task,
            # which is what makes an interruption immediate rather than
            # waiting out audio the caller has already talked over.
            await self.drain()

    async def _report_timings(self, tts_first_audio_ms: float | None) -> None:
        """Attach the speech stages to the turn that produced them.

        Reported before the drain, so a barge-in that cancels playback still
        records how long the agent took to start speaking -- the number is no
        less true for the caller having interrupted it.
        """
        turn_id = self.manager.last_turn_id
        if self.timings_sink is None or turn_id is None:
            return
        stt_ms, self._pending_stt_ms = self._pending_stt_ms, None
        try:
            await self.timings_sink(turn_id, stt_ms, tts_first_audio_ms)
        except Exception as exc:
            logger.warning("voice_timings_report_failed", turn_id=turn_id, error=str(exc))
