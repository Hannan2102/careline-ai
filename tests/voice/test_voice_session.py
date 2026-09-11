"""A whole voice call, with no audio hardware and no vendor (Phase 13).

Audio in is a list of byte chunks; the STT provider is scripted; TTS emits
silence-shaped bytes. What is actually being tested is the wiring: that the
same orchestrator answers, that spending caps bite while the call is running,
and that a call always ends.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from tests.conftest import JOHN_SMITH_DOB

from app.agents.factory import build_runtime
from app.agents.orchestrator import Orchestrator
from app.agents.state import SessionChannel, SessionState
from app.ai.budget_guard import BudgetGuard
from app.ai.providers.base import Transcript, VoiceSpec
from app.ai.usage import TTS_CHARACTERS, UsageLedger
from app.config.settings import Settings
from app.ehr.base import EHRProvider
from app.voice.models import CloseReason
from app.voice.session import BUDGET_MESSAGE, TICK_INTERVAL_SECONDS, VoiceSession
from app.voice.turn_manager import TurnTimings

T0 = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
IDENTIFY = f"My name is John Smith and I was born {JOHN_SMITH_DOB:%d %B %Y}"

#: A voice session drives its own ticker, so these tests run against the real
#: clock -- the one place in the suite that is unavoidable. Thresholds are
#: therefore short enough that a whole call finishes in under two seconds, and
#: far enough apart that no test races them.
FAST = TurnTimings(
    end_of_utterance=timedelta(milliseconds=1),
    silence_prompt=timedelta(seconds=30),
    silence_close=timedelta(seconds=60),
    max_call=timedelta(seconds=30),
)


class ScriptedSTT:
    """Yields a fixed list of finals, one per audio chunk consumed."""

    name = "mock"

    def __init__(self, utterances: list[str]) -> None:
        self.utterances = utterances

    async def transcribe_stream(self, audio: AsyncIterator[bytes]) -> AsyncIterator[Transcript]:
        index = 0
        async for _chunk in audio:
            if index >= len(self.utterances):
                return
            yield Transcript(
                text=self.utterances[index], is_final=True, confidence=0.95, audio_seconds=1.0
            )
            index += 1


class CountingTTS:
    """Emits one chunk per call and records the text, so cost can be asserted."""

    name = "elevenlabs"

    def __init__(self, ledger: UsageLedger, session_id: str) -> None:
        self.ledger = ledger
        self.session_id = session_id
        self.spoken: list[str] = []

    async def synthesize_stream(self, text: str, voice: VoiceSpec) -> AsyncIterator[bytes]:
        self.spoken.append(text)
        self.ledger.record(self.name, TTS_CHARACTERS, len(text), self.session_id)
        yield b"\x00" * 160


#: Slightly longer than the tick interval, so timers advance between
#: utterances the way they would with audio arriving in real time. Yielding
#: every chunk at once would buffer four separate utterances into one.
CHUNK_GAP_SECONDS = TICK_INTERVAL_SECONDS * 1.5


async def chunks(count: int) -> AsyncIterator[bytes]:
    for _ in range(count):
        yield b"\x00" * 320
        await asyncio.sleep(CHUNK_GAP_SECONDS)


@pytest.fixture
def orchestrator(memory_ehr: EHRProvider) -> Orchestrator:
    return build_runtime(
        ehr=memory_ehr, settings=Settings(_env_file=None, app_env="test")
    ).orchestrator


def build(
    orchestrator: Orchestrator,
    utterances: list[str],
    ledger: UsageLedger | None = None,
    settings: Settings | None = None,
) -> tuple[VoiceSession, CountingTTS, list[bytes]]:
    resolved = settings or Settings(_env_file=None, app_env="test")
    usage = ledger or UsageLedger()
    session = SessionState(session_id="sess-voice", channel=SessionChannel.VOICE, created_at=T0)
    tts = CountingTTS(usage, session.session_id)
    out: list[bytes] = []

    async def sink(chunk: bytes) -> None:
        out.append(chunk)

    voice_session = VoiceSession(
        session=session,
        orchestrator=orchestrator,
        stt=ScriptedSTT(utterances),  # type: ignore[arg-type]
        tts=tts,  # type: ignore[arg-type]
        audio_out=sink,
        guard=BudgetGuard(resolved, usage),
        timings=FAST,
    )
    return voice_session, tts, out


class TestAWholeCall:
    async def test_a_booking_completes_by_voice(self, orchestrator: Orchestrator) -> None:
        """The acceptance criterion, without a microphone."""
        voice, tts, out = build(
            orchestrator,
            [
                "Hi, I'd like to schedule a diabetes follow-up with Dr. Patel next week",
                IDENTIFY,
                "The first one please",
                "Yes",
            ],
        )
        await voice.run(chunks(4))

        assert voice.session.is_verified
        assert "booked" in tts.spoken[-1].lower()
        assert out, "audio was emitted"

    async def test_audio_reaches_the_sink(self, orchestrator: Orchestrator) -> None:
        voice, _tts, out = build(orchestrator, ["Are you open on Saturday?"])
        await voice.run(chunks(1))
        assert b"".join(out) != b""

    async def test_the_call_closes_when_the_caller_goes_away(
        self, orchestrator: Orchestrator
    ) -> None:
        voice, _tts, _out = build(orchestrator, ["Are you open on Saturday?"])
        await voice.run(chunks(1))
        assert voice.manager.close_reason is CloseReason.CALLER_HUNG_UP

    async def test_the_same_orchestrator_answers_as_in_text_mode(
        self, orchestrator: Orchestrator
    ) -> None:
        voice, tts, _out = build(orchestrator, ["Are you open on Saturday?"])
        await voice.run(chunks(1))
        assert "closed on Saturday" in tts.spoken[0]

    async def test_a_clinical_question_is_refused_by_voice(
        self, orchestrator: Orchestrator
    ) -> None:
        voice, tts, _out = build(orchestrator, ["Can I double the dose of my lisinopril?"])
        await voice.run(chunks(1))
        spoken = " ".join(tts.spoken).lower()
        assert "nurse" in spoken or "clinical" in spoken
        assert "double" not in spoken


class TestLiveCostCap:
    """The cap has to bite *during* a call, not after it (COSTS.md)."""

    def _capped(self) -> Settings:
        return Settings(
            _env_file=None,
            app_env="test",
            tts_provider="elevenlabs",
            elevenlabs_api_key="el",
            elevenlabs_voice_id="v",
            text_only_mode=False,
            voice_enabled=True,
            tts_enabled=True,
            max_estimated_session_cost_usd=Decimal("0.001"),
        )

    async def test_the_session_cap_ends_the_call(self, orchestrator: Orchestrator) -> None:
        ledger = UsageLedger()
        # Already over the per-session cap before the call starts.
        ledger.record("elevenlabs", TTS_CHARACTERS, 5000, "sess-voice")

        voice, tts, _out = build(
            orchestrator, ["Are you open on Saturday?"], ledger, self._capped()
        )
        await voice.run(chunks(1))

        assert voice.budget_exhausted is True
        assert tts.spoken == [BUDGET_MESSAGE]
        assert voice.manager.close_reason in {CloseReason.BUDGET, CloseReason.CALLER_HUNG_UP}

    async def test_the_caller_is_offered_a_human_not_hung_up_on(
        self, orchestrator: Orchestrator
    ) -> None:
        ledger = UsageLedger()
        ledger.record("elevenlabs", TTS_CHARACTERS, 5000, "sess-voice")
        voice, tts, _out = build(
            orchestrator, ["Are you open on Saturday?"], ledger, self._capped()
        )
        await voice.run(chunks(1))
        assert "member of staff" in tts.spoken[0]

    async def test_nothing_further_is_synthesised_once_exhausted(
        self, orchestrator: Orchestrator
    ) -> None:
        """The cap message must not itself be repeated on every turn."""
        ledger = UsageLedger()
        ledger.record("elevenlabs", TTS_CHARACTERS, 5000, "sess-voice")
        voice, tts, _out = build(
            orchestrator,
            ["Are you open on Saturday?", "Where are you located?"],
            ledger,
            self._capped(),
        )
        await voice.run(chunks(2))
        assert tts.spoken == [BUDGET_MESSAGE]

    async def test_a_call_within_budget_is_not_interrupted(
        self, orchestrator: Orchestrator
    ) -> None:
        voice, tts, _out = build(orchestrator, ["Are you open on Saturday?"], UsageLedger())
        await voice.run(chunks(1))
        assert voice.budget_exhausted is False
        assert len(tts.spoken) == 1


class TestLatencyInstrumentation:
    """The two stages only voice has (Phase 14).

    `stt_ms` and `tts_first_audio_ms` have existed on the trace, the turn row
    and the dashboard since Phase 11 -- and were never written by anything, so
    every turn showed a blank where the speech latency should be. Phase 14
    begins by measuring, and this is the measurement.
    """

    @staticmethod
    def _collector() -> tuple[list[tuple[str, float | None, float | None]], object]:
        seen: list[tuple[str, float | None, float | None]] = []

        async def sink(turn_id: str, stt_ms: float | None, tts_ms: float | None) -> None:
            seen.append((turn_id, stt_ms, tts_ms))

        return seen, sink

    @pytest.mark.asyncio
    async def test_both_stages_are_reported_against_the_turn(
        self, orchestrator: Orchestrator
    ) -> None:
        seen, sink = self._collector()
        voice, _tts, _out = build(orchestrator, ["Are you open on Saturday?"])
        voice.timings_sink = sink  # type: ignore[assignment]

        await voice.run(chunks(1))

        assert len(seen) >= 1, "no speech latency was reported at all"
        turn_id, stt_ms, tts_ms = seen[0]
        assert turn_id.startswith("turn-"), f"reported against {turn_id!r}, not a turn"
        assert stt_ms is not None and stt_ms >= 0.0
        assert tts_ms is not None and tts_ms >= 0.0

    @pytest.mark.asyncio
    async def test_the_turn_id_matches_the_turn_that_was_run(
        self, orchestrator: Orchestrator
    ) -> None:
        """A latency attached to the wrong turn is worse than none.

        The whole point of Phase 14 is that an optimisation cites a
        before/after from real turns, which requires knowing which turn.
        """
        seen, sink = self._collector()
        voice, _tts, _out = build(orchestrator, ["Are you open on Saturday?"])
        voice.timings_sink = sink  # type: ignore[assignment]

        await voice.run(chunks(1))

        assert seen[0][0] == voice.manager.last_turn_id

    @pytest.mark.asyncio
    async def test_a_session_without_a_sink_still_runs(self, orchestrator: Orchestrator) -> None:
        """Persistence is optional, so the measurement has to be too."""
        voice, tts, _out = build(orchestrator, ["Are you open on Saturday?"])
        assert voice.timings_sink is None
        await voice.run(chunks(1))
        assert tts.spoken, "the call did not complete without a timings sink"

    @pytest.mark.asyncio
    async def test_a_failing_sink_does_not_break_the_call(self, orchestrator: Orchestrator) -> None:
        """A caller is on the phone; a latency number is never worth a dropped call."""

        async def exploding(turn_id: str, stt_ms: float | None, tts_ms: float | None) -> None:
            raise RuntimeError("metrics backend is down")

        voice, tts, _out = build(orchestrator, ["Are you open on Saturday?"])
        voice.timings_sink = exploding  # type: ignore[assignment]

        await voice.run(chunks(1))

        assert tts.spoken, "a broken metrics sink took the call down with it"
