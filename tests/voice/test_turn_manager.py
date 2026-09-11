"""Conversational timing (Phase 13).

Everything voice adds over text is timing, and timing bugs are the ones that
make a demo unwatchable: talking over the caller, cutting them off mid-date-of-
birth, or booking twice because two turns raced.

Time is injected throughout. Nothing here sleeps, so the whole file runs in
milliseconds and can be run on every save.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from tests.conftest import JOHN_SMITH_DOB

from app.agents.factory import build_runtime
from app.agents.orchestrator import Orchestrator
from app.agents.state import SessionChannel, SessionState
from app.ai.providers.base import Transcript
from app.config.settings import Settings
from app.ehr.base import EHRProvider
from app.voice.models import CloseReason, VoiceState
from app.voice.turn_manager import TurnManager

T0 = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
IDENTIFY = f"My name is John Smith and I was born {JOHN_SMITH_DOB:%d %B %Y}"


def final(text: str) -> Transcript:
    return Transcript(text=text, is_final=True, confidence=0.95, audio_seconds=1.2)


def ended(text: str) -> Transcript:
    """A final that also carries the recogniser's end-of-speech decision."""
    return Transcript(
        text=text, is_final=True, speech_final=True, confidence=0.95, audio_seconds=1.2
    )


def interim(text: str) -> Transcript:
    return Transcript(text=text, is_final=False, confidence=0.4, audio_seconds=0.4)


class Speaker:
    """A speak function that records what was said and can be made slow.

    ``block`` holds playback open so a test can interrupt it deterministically,
    rather than racing a real audio stream.
    """

    def __init__(self) -> None:
        self.said: list[str] = []
        self.completed: list[str] = []
        self.block: asyncio.Event | None = None

    async def __call__(self, text: str) -> None:
        self.said.append(text)
        if self.block is not None:
            await self.block.wait()
        self.completed.append(text)


@pytest.fixture
def orchestrator(memory_ehr: EHRProvider) -> Orchestrator:
    return build_runtime(
        ehr=memory_ehr, settings=Settings(_env_file=None, app_env="test")
    ).orchestrator


@pytest.fixture
def speaker() -> Speaker:
    return Speaker()


@pytest.fixture
def manager(orchestrator: Orchestrator, speaker: Speaker) -> TurnManager:
    session = SessionState(session_id="sess-voice", channel=SessionChannel.VOICE, created_at=T0)
    turn_manager = TurnManager(session=session, orchestrator=orchestrator, speak=speaker)
    turn_manager.start(now=T0)
    return turn_manager


async def say(manager: TurnManager, text: str, at: datetime) -> None:
    """One complete utterance: the transcript, then the quiet that ends it."""
    await manager.on_transcript(final(text), now=at)
    await manager.tick(now=at + manager.timings.end_of_utterance)


class TestEndOfUtterance:
    async def test_a_final_transcript_is_not_acted_on_immediately(
        self, manager: TurnManager, speaker: Speaker
    ) -> None:
        """Callers pause mid-sentence. Acting on the first final cuts them off."""
        await manager.on_transcript(final("Are you open"), now=T0)
        assert speaker.said == []
        assert manager.stats.turns == 0

    async def test_quiet_after_a_final_ends_the_turn(
        self, manager: TurnManager, speaker: Speaker
    ) -> None:
        await say(manager, "Are you open on Saturday?", T0)
        assert manager.stats.turns == 1
        assert "closed on Saturday" in speaker.said[0]

    async def test_two_finals_in_one_breath_become_one_utterance(
        self, manager: TurnManager, speaker: Speaker
    ) -> None:
        """ "My name is John Smith" / "born 15 February 1985" is one answer."""
        await manager.on_transcript(final("My name is John Smith and"), now=T0)
        await manager.on_transcript(
            final("I was born 15 February 1985"), now=T0 + timedelta(milliseconds=400)
        )
        # Past the fallback window, which is now generous on purpose: without
        # an end-of-speech signal the only safe assumption is that a caller
        # who has paused may not have finished.
        await manager.tick(now=T0 + timedelta(milliseconds=2400))
        assert manager.stats.turns == 1

    async def test_end_of_speech_ends_the_turn_without_waiting(
        self, manager: TurnManager, speaker: Speaker
    ) -> None:
        """The recogniser saying "they stopped" beats any timer.

        Found live: the caller said "my full name is", paused to think, and was
        answered three times before they could say the name. `is_final` marks a
        *segment* as settled and is raised at every pause; `speech_final` is
        the recogniser's end-of-speech decision, and only that one means the
        sentence is over.
        """
        await manager.on_transcript(ended("Are you open on Saturday?"), now=T0)
        await manager.tick(now=T0 + timedelta(milliseconds=100))
        assert manager.stats.turns == 1, "waited on a timer despite end-of-speech"

    async def test_a_pause_mid_sentence_does_not_end_the_turn(
        self, manager: TurnManager, speaker: Speaker
    ) -> None:
        """The exact shape of the live failure, as a test."""
        await manager.on_transcript(final("Yeah. My full name is"), now=T0)
        await manager.tick(now=T0 + timedelta(milliseconds=900))
        assert manager.stats.turns == 0, "answered a caller who was mid-sentence"

        await manager.on_transcript(ended("Linda Nguyen"), now=T0 + timedelta(seconds=1))
        await manager.tick(now=T0 + timedelta(milliseconds=1100))
        assert manager.stats.turns == 1
        # Both halves reached the orchestrator as one utterance, which is the
        # point: the pause split the transcript, not the sentence.
        started = [e.detail or "" for e in manager.stats.events if e.kind == "turn_started"]
        assert started == ["Yeah. My full name is Linda Nguyen"]

    async def test_interim_transcripts_never_reach_the_orchestrator(
        self, manager: TurnManager, speaker: Speaker
    ) -> None:
        """An interim is a guess the recogniser is about to revise."""
        await manager.on_transcript(interim("are you open on sat"), now=T0)
        await manager.tick(now=T0 + timedelta(seconds=2))
        assert manager.stats.turns == 0
        assert manager.stats.discarded_interims == 1
        assert speaker.said == []

    async def test_an_empty_transcript_is_ignored(self, manager: TurnManager) -> None:
        await manager.on_transcript(final("   "), now=T0)
        await manager.tick(now=T0 + timedelta(seconds=2))
        assert manager.stats.turns == 0


class TestBargeIn:
    async def test_speech_during_playback_cancels_it(
        self, manager: TurnManager, speaker: Speaker
    ) -> None:
        speaker.block = asyncio.Event()
        turn = asyncio.create_task(say(manager, "Are you open on Saturday?", T0))
        await asyncio.sleep(0)  # let playback start
        while not speaker.said:
            await asyncio.sleep(0)

        await manager.on_transcript(interim("actually"), now=T0 + timedelta(seconds=1))
        await turn

        assert manager.stats.barge_ins == 1
        assert speaker.said, "the agent started speaking"
        assert speaker.completed == [], "and was cut off before finishing"

    async def test_an_interim_is_enough_to_interrupt(
        self, manager: TurnManager, speaker: Speaker
    ) -> None:
        """Waiting for a final would mean talking over the caller's first phrase."""
        speaker.block = asyncio.Event()
        turn = asyncio.create_task(say(manager, "Are you open on Saturday?", T0))
        while not speaker.said:
            await asyncio.sleep(0)

        await manager.on_transcript(interim("wait"), now=T0 + timedelta(seconds=1))
        await turn
        assert manager.stats.barge_ins == 1

    async def test_a_cough_does_not_interrupt(self, manager: TurnManager, speaker: Speaker) -> None:
        """Sub-threshold noise is not an interruption."""
        speaker.block = asyncio.Event()
        turn = asyncio.create_task(say(manager, "Are you open on Saturday?", T0))
        while not speaker.said:
            await asyncio.sleep(0)

        await manager.on_transcript(interim("a"), now=T0 + timedelta(seconds=1))
        assert manager.stats.barge_ins == 0
        speaker.block.set()
        await turn

    async def test_the_state_returns_to_listening_after_an_interruption(
        self, manager: TurnManager, speaker: Speaker
    ) -> None:
        speaker.block = asyncio.Event()
        turn = asyncio.create_task(say(manager, "Are you open on Saturday?", T0))
        while not speaker.said:
            await asyncio.sleep(0)
        await manager.on_transcript(interim("actually"), now=T0 + timedelta(seconds=1))
        await turn
        assert manager.state is VoiceState.LISTENING


class TestSilence:
    async def test_silence_prompts_once(self, manager: TurnManager, speaker: Speaker) -> None:
        await manager.tick(now=T0 + timedelta(seconds=9))
        assert speaker.said == ["Are you still there?"]
        assert manager.stats.silence_prompts == 1

    async def test_the_prompt_is_not_repeated(self, manager: TurnManager, speaker: Speaker) -> None:
        await manager.tick(now=T0 + timedelta(seconds=9))
        await manager.tick(now=T0 + timedelta(seconds=12))
        assert manager.stats.silence_prompts == 1

    async def test_continued_silence_closes_the_call(
        self, manager: TurnManager, speaker: Speaker
    ) -> None:
        await manager.tick(now=T0 + timedelta(seconds=9))  # "Are you still there?"
        # `silence_close` runs from the end of the re-prompt, not from the same
        # origin as `silence_prompt` -- which is what its docstring always
        # claimed and what the caller experiences. The tick *after* playback is
        # what re-anchors it, so it takes one beat; at the production rate that
        # is 100 ms, and this test ticks at that rate rather than pretending
        # the anchor is instant.
        await manager.tick(now=T0 + timedelta(seconds=9, milliseconds=100))
        await manager.tick(now=T0 + timedelta(seconds=20))
        assert manager.state is VoiceState.CLOSED
        assert manager.close_reason is CloseReason.SILENCE
        assert "call back" in speaker.said[-1]

    async def test_speaking_resets_the_silence_timer(
        self, manager: TurnManager, speaker: Speaker
    ) -> None:
        await manager.tick(now=T0 + timedelta(seconds=9))
        await say(manager, "Sorry, I'm here. Where are you located?", T0 + timedelta(seconds=10))
        await manager.tick(now=T0 + timedelta(seconds=15))
        assert manager.state is not VoiceState.CLOSED


class TestOurOwnSpeechIsNotTheCallersSilence:
    """The agent talking is not the caller failing to talk.

    Every other double in this file returns from `speak` instantly, so the
    agent's utterances cost no clock time and the silence timers never see
    them. That made the whole suite blind to the bug this class covers: a
    sixteen-second list of appointment slots spent an eight-second silence
    budget before the caller had heard the end of it, so the agent asked "are
    you still there?" the moment it stopped talking and hung up on someone who
    was never given a turn.
    """

    #: Characters per second of speech. A calm receptionist; the real offer
    #: message measured ~16s against Groq, which this rate reproduces.
    CHARS_PER_SECOND = 15.0

    class RealTimeSpeaker:
        """Speech that costs the clock what it would cost out loud."""

        def __init__(self, rate: float) -> None:
            self.said: list[str] = []
            self.rate = rate
            self.clock = T0

        async def __call__(self, text: str) -> None:
            self.said.append(text)
            self.clock += timedelta(seconds=len(text) / self.rate)

    @pytest.fixture
    def slow_speaker(self) -> RealTimeSpeaker:
        return self.RealTimeSpeaker(self.CHARS_PER_SECOND)

    @pytest.fixture
    def manager(self, orchestrator: Orchestrator, slow_speaker: RealTimeSpeaker) -> TurnManager:
        session = SessionState(session_id="sess-slow", channel=SessionChannel.VOICE, created_at=T0)
        turn_manager = TurnManager(session=session, orchestrator=orchestrator, speak=slow_speaker)
        turn_manager.start(now=T0)
        return turn_manager

    async def _turn(
        self, manager: TurnManager, speaker: RealTimeSpeaker, text: str, at: datetime
    ) -> datetime:
        """One exchange. Returns the moment the agent stopped speaking."""
        await manager.on_transcript(final(text), now=at)
        ended = at + manager.timings.end_of_utterance
        speaker.clock = ended
        await manager.tick(now=ended)
        return speaker.clock

    async def test_a_long_reply_does_not_spend_the_callers_silence_budget(
        self, manager: TurnManager, slow_speaker: RealTimeSpeaker
    ) -> None:
        """The offer message is the first reply long enough to exceed both
        thresholds on its own, which is why the call died exactly there."""
        now = T0
        for line in ["I need to book an appointment", "My name is John Smith", IDENTIFY]:
            now = await self._turn(manager, slow_speaker, line, now)

        offer = slow_speaker.said[-1]
        spoken = len(offer) / self.CHARS_PER_SECOND
        assert spoken > manager.timings.silence_prompt.total_seconds(), (
            f"the offer is only {spoken:.1f}s; it no longer reproduces the case"
        )

        await manager.tick(now=now + timedelta(milliseconds=100))
        assert manager.stats.silence_prompts == 0
        assert manager.state is not VoiceState.CLOSED

    async def test_the_caller_still_gets_the_full_window_after_a_long_reply(
        self, manager: TurnManager, slow_speaker: RealTimeSpeaker
    ) -> None:
        """Not merely delayed -- the whole budget, counted from the end."""
        now = T0
        for line in ["I need to book an appointment", "My name is John Smith", IDENTIFY]:
            now = await self._turn(manager, slow_speaker, line, now)

        await manager.tick(now=now + timedelta(milliseconds=100))  # re-anchors here
        anchor = now + timedelta(milliseconds=100)

        await manager.tick(now=anchor + manager.timings.silence_prompt - timedelta(seconds=1))
        assert manager.stats.silence_prompts == 0, "prompted a second early"

        await manager.tick(now=anchor + manager.timings.silence_prompt)
        assert manager.stats.silence_prompts == 1
        assert slow_speaker.said[-1] == "Are you still there?"

    async def test_a_genuinely_silent_caller_is_still_prompted_and_released(
        self, manager: TurnManager, slow_speaker: RealTimeSpeaker
    ) -> None:
        """The fix must not make the agent wait forever on an empty line."""
        now = await self._turn(manager, slow_speaker, "I need to book an appointment", T0)

        await manager.tick(now=now + timedelta(milliseconds=100))
        anchor = now + timedelta(milliseconds=100)
        await manager.tick(now=anchor + manager.timings.silence_prompt)
        assert manager.stats.silence_prompts == 1

        prompt_ended = slow_speaker.clock
        await manager.tick(now=prompt_ended + timedelta(milliseconds=100))
        await manager.tick(
            now=prompt_ended + timedelta(milliseconds=100) + manager.timings.silence_close
        )
        assert manager.state is VoiceState.CLOSED
        assert manager.close_reason is CloseReason.SILENCE

    async def test_a_closed_call_ignores_everything_after(
        self, manager: TurnManager, speaker: Speaker
    ) -> None:
        await manager.close(CloseReason.CALLER_HUNG_UP)
        spoken = len(speaker.said)
        await say(manager, "Are you open on Saturday?", T0 + timedelta(seconds=1))
        assert manager.stats.turns == 0
        assert len(speaker.said) == spoken


class TestTimeout:
    async def test_a_call_has_a_hard_limit(self, manager: TurnManager, speaker: Speaker) -> None:
        await manager.tick(now=T0 + timedelta(minutes=11))
        assert manager.state is VoiceState.CLOSED
        assert manager.close_reason is CloseReason.TIMEOUT
        assert "time limit" in speaker.said[-1]

    async def test_closing_is_idempotent(self, manager: TurnManager) -> None:
        closes: list[CloseReason] = []
        manager.on_close = closes.append.__call__  # type: ignore[assignment]

        async def record(reason: CloseReason) -> None:
            closes.append(reason)

        manager.on_close = record
        await manager.close(CloseReason.COMPLETED)
        await manager.close(CloseReason.SILENCE)
        assert closes == [CloseReason.COMPLETED]


class SlowOrchestrator:
    """An orchestrator that can be held mid-turn.

    Queuing happens while the agent is *thinking* -- waiting on the EHR, say --
    not while it is speaking: speech during playback is a barge-in, which
    cancels playback and frees the turn immediately. Blocking the speaker
    therefore cannot exercise this path, and a test that tried would hang.
    """

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.utterances: list[str] = []

    async def handle_turn(
        self, session: SessionState, utterance: str, now: datetime | None = None
    ) -> object:
        self.utterances.append(utterance)
        await self.release.wait()

        class _Trace:
            # The manager reads the turn id off the trace so the transport can
            # attribute its speech latencies to the turn (Phase 14). A double
            # standing in for TurnResult has to carry one.
            turn_id = "turn-double"

        class _Result:
            message = "All right."
            trace = _Trace()

        return _Result()


class TestOneTurnAtATime:
    @pytest.fixture
    def slow(self, speaker: Speaker) -> tuple[TurnManager, SlowOrchestrator]:
        thinking = SlowOrchestrator()
        session = SessionState(session_id="sess-voice", channel=SessionChannel.VOICE, created_at=T0)
        manager = TurnManager(
            session=session,
            orchestrator=thinking,  # type: ignore[arg-type]
            speak=speaker,
        )
        manager.start(now=T0)
        return manager, thinking

    async def test_a_final_arriving_mid_turn_is_queued_not_raced(
        self, slow: tuple[TurnManager, SlowOrchestrator]
    ) -> None:
        """Two concurrent turns on one session is how a caller gets booked twice."""
        manager, thinking = slow
        first = asyncio.create_task(say(manager, "Are you open on Saturday?", T0))
        while not thinking.utterances:
            await asyncio.sleep(0)

        at = T0 + timedelta(seconds=2)
        await manager.on_transcript(final("Where are you located?"), now=at)
        await manager.tick(now=at + manager.timings.end_of_utterance)

        assert manager.stats.queued_finals == 1
        assert thinking.utterances == ["Are you open on Saturday?"]

        thinking.release.set()
        await first

    async def test_a_queued_utterance_runs_afterwards(
        self, slow: tuple[TurnManager, SlowOrchestrator]
    ) -> None:
        manager, thinking = slow
        first = asyncio.create_task(say(manager, "Are you open on Saturday?", T0))
        while not thinking.utterances:
            await asyncio.sleep(0)

        at = T0 + timedelta(seconds=2)
        await manager.on_transcript(final("Where are you located?"), now=at)
        await manager.tick(now=at + manager.timings.end_of_utterance)
        thinking.release.set()
        await first

        await manager.tick(now=at + timedelta(seconds=3))
        assert thinking.utterances == ["Are you open on Saturday?", "Where are you located?"]
        assert manager.stats.turns == 2


class TestVoiceChangesNoBusinessLogic:
    """The acceptance criterion that matters most (ADR 005)."""

    async def test_a_booking_completes_by_voice(
        self, manager: TurnManager, speaker: Speaker
    ) -> None:
        script = [
            "Hi, I'd like to schedule a diabetes follow-up with Dr. Patel next week",
            IDENTIFY,
            "The first one please",
            "Yes",
        ]
        moment = T0
        for utterance in script:
            await say(manager, utterance, moment)
            moment += timedelta(seconds=5)

        assert manager.stats.turns == 4
        assert manager.session.is_verified
        assert "booked" in speaker.said[-1].lower()

    async def test_a_clinical_question_is_refused_by_voice_too(
        self, manager: TurnManager, speaker: Speaker
    ) -> None:
        await say(manager, "Can I double the dose of my lisinopril?", T0)
        spoken = speaker.said[0].lower()
        assert "nurse" in spoken or "clinical" in spoken
        assert "double" not in spoken

    async def test_the_turn_manager_holds_no_clinical_state(self, manager: TurnManager) -> None:
        """Its attributes are timing and transport, never the record."""
        held = {k for k, v in vars(manager).items() if isinstance(v, str | list)}
        assert "patient_ref" not in held
        assert not any("medication" in name for name in held)


class TestSpeakingFirst:
    """The agent opens the call, and that is not a turn.

    A line that opens in silence leaves the caller saying "hello?" into it, and
    "hello?" carries no intent -- so the first thing they say is spent finding
    out whether anyone is there.
    """

    async def test_the_greeting_is_spoken(self, manager: TurnManager, speaker: Speaker) -> None:
        await manager.greet("Thank you for calling.")
        assert speaker.said == ["Thank you for calling."]
        assert manager.state is VoiceState.LISTENING

    async def test_it_is_not_counted_as_a_turn(self, manager: TurnManager) -> None:
        """Nothing was said to classify, so the orchestrator is never called."""
        await manager.greet("Thank you for calling.")
        assert manager.stats.turns == 0

    async def test_the_caller_can_talk_over_it(
        self, manager: TurnManager, speaker: Speaker
    ) -> None:
        """Someone who calls every week should not have to hear it out."""
        speaker.block = asyncio.Event()
        greeting = asyncio.create_task(manager.greet("Thank you for calling."))
        await asyncio.sleep(0)
        assert manager.state is VoiceState.SPEAKING

        await manager.on_transcript(interim("I need to cancel"), now=T0)

        await greeting
        assert manager.stats.barge_ins == 1
        assert "Thank you for calling." not in speaker.completed

    async def test_the_silence_window_starts_when_the_greeting_ends(
        self, manager: TurnManager, speaker: Speaker
    ) -> None:
        """Otherwise the agent greets the caller and then asks if they left.

        The greeting is several seconds of the agent talking. Measured from the
        start of the call, it spends most of the caller's first silence window
        before they have had a chance to use it -- the same bug that made a
        long list of appointment slots hang up on people.
        """
        await manager.greet("Thank you for calling.")
        # The greeting ended here, as far as the manager can tell: it does not
        # read the clock, so the first tick after playback is what dates it.
        await manager.tick(now=T0 + timedelta(seconds=7))

        await manager.tick(now=T0 + timedelta(seconds=14))

        assert manager.stats.silence_prompts == 0, (
            "the agent asked whether the caller was still there seven seconds "
            "after it stopped talking to them"
        )

    async def test_silence_after_the_greeting_is_still_noticed(
        self, manager: TurnManager, speaker: Speaker
    ) -> None:
        """Re-anchoring must delay the prompt, not disable it."""
        await manager.greet("Thank you for calling.")
        await manager.tick(now=T0 + timedelta(seconds=7))

        await manager.tick(now=T0 + timedelta(seconds=16))

        assert speaker.said[-1] == "Are you still there?"
