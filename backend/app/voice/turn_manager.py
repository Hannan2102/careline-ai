"""Conversational timing.

Everything voice adds over text is here, and it is deliberately *only* timing:
when an utterance has ended, when to interrupt playback, when silence means
"they're thinking" versus "they've gone", and what to do about a transcript
that arrives while the previous turn is still running. What the agent decides
is not this module's business -- it calls the same orchestrator text mode
calls, which is what keeps the two from drifting (ADR 005).

Time is injected, never read from the clock. A turn manager that calls
``datetime.now()`` internally can only be tested by sleeping, and a test suite
that sleeps is a test suite nobody runs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.agents.orchestrator import Orchestrator, TurnResult
from app.agents.state import SessionState
from app.ai.providers.base import Transcript
from app.observability.logging import get_logger
from app.voice.models import CloseReason, SpeakFn, VoiceEvent, VoiceState

logger = get_logger(__name__)


@dataclass(frozen=True)
class TurnTimings:
    """Thresholds the conversation is judged against.

    Defaults come from docs/voice-architecture.md and are deliberately
    generous: a patient reading a date of birth off a card is not the same
    speaker as one confirming "yes", and cutting off the first is worse than
    waiting on the second.
    """

    #: Quiet after a final transcript before we treat the turn as over.
    #:
    #: A fallback, not the main path. A recogniser that reports end-of-speech
    #: ends the turn the moment it says so; this is what decides when nothing
    #: ever says so. It is therefore generous on purpose: the cost of waiting
    #: too long is a beat of dead air, and the cost of not waiting long enough
    #: is answering half a sentence -- which is what 800 ms did to a caller
    #: pausing in the middle of "my full name is ... Linda Nguyen".
    end_of_utterance: timedelta = timedelta(milliseconds=1800)
    #: Quiet with nothing said at all before we re-prompt. Measured from the
    #: moment the caller could actually have started speaking -- the end of our
    #: own last utterance -- not from the last thing they said.
    silence_prompt: timedelta = timedelta(seconds=8)
    #: Quiet after re-prompting before we offer a human and close. Measured
    #: from the end of the re-prompt, which is what it has always claimed to
    #: mean; it used to be measured from the same origin as `silence_prompt`,
    #: making it a total rather than a follow-on.
    silence_close: timedelta = timedelta(seconds=10)
    #: Total call length, whatever is happening.
    max_call: timedelta = timedelta(minutes=10)
    #: Speech shorter than this during playback is treated as a noise, not an
    #: interruption -- a cough should not cancel the agent mid-sentence.
    barge_in_min_characters: int = 2


@dataclass
class TurnManagerStats:
    """Counters worth having when a call goes wrong."""

    turns: int = 0
    barge_ins: int = 0
    silence_prompts: int = 0
    discarded_interims: int = 0
    queued_finals: int = 0
    events: list[VoiceEvent] = field(default_factory=list)


class TurnManager:
    """Drives one voice conversation.

    The transport feeds it transcripts and ticks; it decides when to run a
    turn, when to stop talking, and when to hang up. It owns no business
    logic and holds no clinical state.
    """

    def __init__(
        self,
        session: SessionState,
        orchestrator: Orchestrator,
        speak: SpeakFn,
        timings: TurnTimings | None = None,
        on_close: Callable[[CloseReason], Awaitable[None]] | None = None,
        on_interrupt: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.session = session
        self.orchestrator = orchestrator
        self.speak = speak
        self.timings = timings or TurnTimings()
        self.on_close = on_close
        #: Called when playback is cut off with audio still in flight.
        #:
        #: Cancelling the speak task stops us *producing* audio, which is all a
        #: test can observe. Over a real transport it is not enough: everything
        #: already handed to the client is sitting in its playback queue, and
        #: barge-in that leaves the agent talking for another two seconds is
        #: not barge-in. The transport uses this to tell the client to drop
        #: what it is holding.
        self.on_interrupt = on_interrupt

        self.state = VoiceState.LISTENING
        self.stats = TurnManagerStats()
        self.close_reason: CloseReason | None = None
        #: The turn the orchestrator most recently produced.
        #:
        #: Exposed so the transport can attribute its own stage latencies to a
        #: turn without the manager having to know what those stages are. Left
        #: set after the turn ends: the reply is spoken afterwards, and that is
        #: precisely when the speech timings become known.
        self.last_turn_id: str | None = None

        self._buffer: list[str] = []
        self._pending: list[str] = []
        self._last_voice_at: datetime | None = None
        self._last_final_at: datetime | None = None
        self._started_at: datetime | None = None
        self._prompted_for_silence = False
        #: Set when the recogniser reports end-of-speech, so the next tick runs
        #: the turn without waiting out the fallback timer.
        self._utterance_ended = False
        #: Set when playback ends, cleared by the next tick, which re-anchors
        #: the silence timers to that moment.
        #:
        #: A flag rather than a timestamp because this module is not allowed to
        #: read the clock: the time playback ended is only knowable to whoever
        #: supplies `now`, and the next tick is the first thing to know it. In
        #: production that lands within one tick (100 ms); in a test it is
        #: exactly the injected moment.
        self._reanchor_silence = False
        self._playback: asyncio.Task[None] | None = None
        #: One in-flight turn per session. A second concurrent turn would let
        #: two workflows mutate the same session, which is how a caller ends
        #: up with two appointments from one sentence.
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ lifecycle
    def start(self, now: datetime | None = None) -> None:
        moment = now or datetime.now(UTC)
        self._started_at = moment
        self._last_voice_at = moment
        self._record("call_started")

    async def close(self, reason: CloseReason) -> None:
        """End the call once. Idempotent, because several things can end it."""
        if self.state is VoiceState.CLOSED:
            return
        await self._stop_playback()
        self.state = VoiceState.CLOSED
        self.close_reason = reason
        self._record("call_closed", str(reason.value))
        logger.info("voice_call_closed", session_id=self.session.session_id, reason=reason.value)
        if self.on_close is not None:
            await self.on_close(reason)

    # --------------------------------------------------------------- input
    async def on_transcript(self, transcript: Transcript, now: datetime | None = None) -> None:
        """Handle one recognition result from the STT provider."""
        if self.state is VoiceState.CLOSED:
            return
        moment = now or datetime.now(UTC)
        text = transcript.text.strip()
        if not text:
            return

        self._last_voice_at = moment
        self._prompted_for_silence = False
        # The caller has spoken, so their own timing is the anchor; a pending
        # re-anchor from the utterance they are talking over must not move it.
        self._reanchor_silence = False

        # Barge-in is decided on *any* speech, interim included: waiting for a
        # final would mean talking over the caller for the length of their
        # first phrase, which is the single rudest thing a voice agent does.
        if self.state is VoiceState.SPEAKING and len(text) >= self.timings.barge_in_min_characters:
            await self._barge_in()

        if not transcript.is_final:
            # Interim results are for display and logs. Acting on one means
            # acting on a guess the recogniser is about to revise.
            self.stats.discarded_interims += 1
            return

        self._buffer.append(text)
        self._last_final_at = moment
        # The recogniser says the caller has stopped talking, so there is
        # nothing to wait for. Segments keep accumulating either way -- an
        # utterance can arrive as several finals, and only the last carries
        # end-of-speech -- so the buffer holds the whole sentence by now.
        if transcript.speech_final:
            self._utterance_ended = True

    async def tick(self, now: datetime | None = None) -> None:
        """Advance timers. The transport calls this on a regular beat."""
        if self.state is VoiceState.CLOSED:
            return
        moment = now or datetime.now(UTC)

        # Playback finished since the last tick, so the caller's chance to
        # speak starts now. Without this the agent's own speaking time counts
        # as the caller's silence: a sixteen-second list of appointment slots
        # exhausts an eight-second budget before the caller has heard the end
        # of it, and the agent asks "are you still there?" the instant it stops
        # talking -- then hangs up on someone who was never given a turn.
        if self._reanchor_silence:
            self._reanchor_silence = False
            self._last_voice_at = moment

        if self._started_at is not None and moment - self._started_at >= self.timings.max_call:
            await self._say("We've reached the time limit for this call. Goodbye.")
            await self.close(CloseReason.TIMEOUT)
            return

        # An utterance is finished when the recogniser says so, or -- for one
        # that does not report end-of-speech -- when it has gone quiet.
        if self._buffer and (
            self._utterance_ended
            or (
                self._last_final_at is not None
                and moment - self._last_final_at >= self.timings.end_of_utterance
            )
        ):
            self._utterance_ended = False
            await self._run_turn(moment)
            return

        if self.state is not VoiceState.LISTENING or self._last_voice_at is None:
            return

        quiet_for = moment - self._last_voice_at
        if not self._prompted_for_silence and quiet_for >= self.timings.silence_prompt:
            self._prompted_for_silence = True
            self.stats.silence_prompts += 1
            self._record("silence_prompt")
            await self._say("Are you still there?")
            return

        if self._prompted_for_silence and quiet_for >= self.timings.silence_close:
            await self._say(
                "I haven't heard anything, so I'll let you go. "
                "Please call back and we'll pick up where we left off."
            )
            await self.close(CloseReason.SILENCE)

    # -------------------------------------------------------------- turns
    async def _run_turn(self, moment: datetime) -> TurnResult | None:
        """Hand a completed utterance to the orchestrator and speak the reply."""
        if self._lock.locked():
            # A final arrived while the previous turn was still running. Queue
            # it rather than racing: two concurrent turns on one session is how
            # a caller gets two appointments from one sentence.
            self._pending.extend(self._buffer)
            self._buffer.clear()
            self.stats.queued_finals += 1
            self._record("final_queued")
            return None

        async with self._lock:
            utterance = " ".join(self._buffer).strip()
            self._buffer.clear()
            if not utterance:
                return None

            self.state = VoiceState.THINKING
            self.stats.turns += 1
            self._record("turn_started", utterance[:80])

            result = await self.orchestrator.handle_turn(self.session, utterance, now=moment)
            # Before speaking, so the speech stages measured during `_say` can
            # be attributed to the turn that caused them.
            self.last_turn_id = result.trace.turn_id
            await self._say(result.message)

            if not self.session.is_active:
                await self.close(CloseReason.COMPLETED)
                return result

        # Anything that arrived mid-turn runs now, outside the lock.
        if self._pending:
            self._buffer.extend(self._pending)
            self._pending.clear()
            self._last_final_at = moment - self.timings.end_of_utterance
        return result

    # ------------------------------------------------------------ playback
    async def _say(self, text: str) -> None:
        """Speak, interruptibly."""
        if self.state is VoiceState.CLOSED or not text.strip():
            return
        await self._stop_playback()
        self.state = VoiceState.SPEAKING
        self._playback = asyncio.create_task(self.speak(text))
        try:
            await self._playback
        except asyncio.CancelledError:
            # Barge-in. Expected, not an error.
            self._record("playback_cancelled")
        finally:
            self._playback = None
            if self.state is VoiceState.SPEAKING:
                self.state = VoiceState.LISTENING
            # In the `finally`, so a barge-in re-anchors too: the caller who
            # interrupted is owed a full window from where we stopped, not
            # from whenever they last managed to finish a sentence.
            self._reanchor_silence = True

    async def _barge_in(self) -> None:
        self.stats.barge_ins += 1
        self._record("barge_in")
        logger.info("voice_barge_in", session_id=self.session.session_id)
        await self._stop_playback()
        self.state = VoiceState.LISTENING

    async def _stop_playback(self) -> None:
        task = self._playback
        if task is None or task.done():
            return
        # Before cancelling, not after: the client should stop playing at the
        # moment the caller interrupted, not once the server has finished
        # unwinding its own task.
        if self.on_interrupt is not None:
            try:
                await self.on_interrupt()
            except Exception as exc:
                logger.warning("voice_interrupt_signal_failed", error=str(exc))
        current = asyncio.current_task()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            # The playback task's own cancellation -- exactly what we asked
            # for. Note that `CancelledError` is a BaseException, so
            # `suppress(Exception)` does *not* catch it: written that way, every
            # barge-in leaks the cancellation into whoever asked us to stop.
            #
            # If this task is itself being cancelled, that is a different thing
            # and must not be swallowed.
            if current is not None and current.cancelling() > 0:
                raise
        except Exception as exc:
            # Stopping must never fail: it runs on barge-in, on close, and
            # before every utterance, and a failure here would strand the call
            # in SPEAKING with nobody talking.
            logger.warning("voice_playback_stop_failed", error=str(exc))

    # -------------------------------------------------------------- record
    def _record(self, kind: str, detail: str | None = None) -> None:
        self.stats.events.append(VoiceEvent(kind=kind, detail=detail, state=self.state))
