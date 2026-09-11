"""The browser transport: the WebSocket wire contract.

Tested through a real ASGI app with a real socket rather than by calling the
handler, because everything worth getting wrong here is a wiring question --
which frames go out, in what order, and whether the client is told to drop its
playback queue when the caller interrupts.

That last one is the reason this file exists. Barge-in has always passed its
unit tests: cancelling the speak task provably stops synthesis. Over a socket
that is only half the job, and the missing half is invisible to every test that
does not have a client holding a buffer.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import voice_ws
from app.voice.models import CloseReason


class FakeWebSocket:
    """The subset of ``WebSocket`` the transport uses.

    A hand-rolled double rather than ``TestClient``: these tests need to inject
    a barge-in at a precise moment relative to playback, which a synchronous
    test client cannot express.
    """

    def __init__(self) -> None:
        self.sent: list[dict | bytes] = []
        self.closed_with: int | None = None

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)

    def events(self, kind: str) -> list[dict]:
        return [m for m in self.sent if isinstance(m, dict) and m.get("type") == kind]

    @property
    def audio(self) -> bytes:
        return b"".join(m for m in self.sent if isinstance(m, bytes))


class TestHangupParsing:
    """The client's only control message. It is parsed, so it can be malformed."""

    def test_recognises_a_hangup(self) -> None:
        assert voice_ws._is_hangup('{"type": "hangup"}')

    @pytest.mark.parametrize(
        "text",
        ["", "not json", "[]", '"hangup"', "null", '{"type": "something-else"}', "{}"],
        ids=["empty", "garbage", "array", "bare-string", "null", "other-type", "no-type"],
    )
    def test_anything_else_is_not_a_hangup(self, text: str) -> None:
        """A malformed frame must not end a call, and must not raise either."""
        assert voice_ws._is_hangup(text) is False


class TestAudioQueue:
    @pytest.mark.asyncio
    async def test_the_sentinel_ends_the_stream(self) -> None:
        """What turns a client disconnect into a closed call.

        Without it the audio iterator never completes and a vanished caller
        leaves a session ticking until its ten-minute limit.
        """
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        await queue.put(b"one")
        await queue.put(b"two")
        await queue.put(None)
        await queue.put(b"never read")
        assert [c async for c in voice_ws._drain(queue)] == [b"one", b"two"]


class TestWireContract:
    """Events reach the client in the shape the browser expects."""

    @pytest.mark.asyncio
    async def test_a_closed_socket_swallows_sends(self) -> None:
        """Every teardown event races the client going away.

        A raise here would replace the real reason a call ended with a noisy
        secondary failure in the logs.
        """
        from starlette.websockets import WebSocketState

        class Disconnected(FakeWebSocket):
            client_state = WebSocketState.DISCONNECTED

        socket = Disconnected()
        await voice_ws._send_json(socket, None, {"type": "closed"})  # type: ignore[arg-type]
        assert socket.sent == []

    @pytest.mark.asyncio
    async def test_sends_are_serialised_by_the_lock(self) -> None:
        """Audio and events come from different tasks; interleaving corrupts a frame."""
        from starlette.websockets import WebSocketState

        class Connected(FakeWebSocket):
            client_state = WebSocketState.CONNECTED

        socket = Connected()
        lock = asyncio.Lock()
        await asyncio.gather(
            *(voice_ws._send_json(socket, lock, {"type": "transcript", "n": i}) for i in range(20))
        )
        assert len(socket.events("transcript")) == 20


class TestTranscriptRelay:
    @pytest.mark.asyncio
    async def test_forwards_every_transcript_and_passes_it_through(self) -> None:
        """Interims included: they are what the caller sees while speaking."""
        from app.ai.providers.base import Transcript

        class Inner:
            name = "mock"

            async def transcribe_stream(self, audio):  # type: ignore[no-untyped-def]
                async for _ in audio:
                    pass
                yield Transcript(text="book an", is_final=False)
                yield Transcript(text="book an appointment", is_final=True)

        seen: list[Transcript] = []

        async def relay(transcript: Transcript) -> None:
            seen.append(transcript)

        async def audio():  # type: ignore[no-untyped-def]
            yield b"\x00\x00"

        relayed = voice_ws._TranscriptRelay(Inner(), relay)  # type: ignore[arg-type]
        out = [t async for t in relayed.transcribe_stream(audio())]

        assert [t.text for t in seen] == ["book an", "book an appointment"]
        assert [t.is_final for t in seen] == [False, True]
        # The relay must not consume what the turn manager needs.
        assert [t.text for t in out] == ["book an", "book an appointment"]

    def test_keeps_the_wrapped_provider_name(self) -> None:
        """The name is what the budget guard and the usage ledger key on."""

        class Inner:
            name = "deepgram"

        relay = voice_ws._TranscriptRelay(Inner(), None)  # type: ignore[arg-type]
        assert relay.name == "deepgram"


class TestVoiceDisabled:
    def test_a_text_only_checkout_is_refused_with_instructions(self) -> None:
        """The default configuration. The message is the only thing that says why."""
        app = FastAPI()
        app.include_router(voice_ws.router)
        with TestClient(app).websocket_connect("/ws/voice") as socket:
            message = socket.receive_json()
        assert message["type"] == "error"
        assert "TEXT_ONLY_MODE=false" in message["detail"]


class TestInterruptSignal:
    """The half of barge-in that only exists over a transport."""

    @pytest.mark.asyncio
    async def test_the_turn_manager_signals_before_it_cancels(self) -> None:
        """Order matters: the client should stop at the moment of interruption.

        Signalling after the server has unwound its own playback task leaves
        the browser playing a buffer the caller has already talked over.
        """
        from app.agents.state import SessionChannel, SessionState
        from app.ai.providers.base import Transcript
        from app.voice.turn_manager import TurnManager

        order: list[str] = []
        speaking = asyncio.Event()

        async def speak(text: str) -> None:
            speaking.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                order.append("cancelled")
                raise

        async def on_interrupt() -> None:
            order.append("signalled")

        session = SessionState(session_id="sess-voice", channel=SessionChannel.VOICE)
        manager = TurnManager(
            session=session,
            orchestrator=None,  # type: ignore[arg-type]
            speak=speak,
            on_interrupt=on_interrupt,
        )
        manager.start()

        say = asyncio.create_task(manager._say("A long sentence the caller talks over."))
        await asyncio.wait_for(speaking.wait(), timeout=1)
        await manager.on_transcript(Transcript(text="actually", is_final=False))
        await say

        assert order == ["signalled", "cancelled"]
        assert manager.stats.barge_ins == 1

    @pytest.mark.asyncio
    async def test_a_failing_signal_does_not_strand_the_call(self) -> None:
        """Stopping playback runs on barge-in, on close, and before every
        utterance. A transport error here must not leave the call in SPEAKING
        with nobody talking."""
        from app.agents.state import SessionChannel, SessionState
        from app.voice.models import VoiceState
        from app.voice.turn_manager import TurnManager

        speaking = asyncio.Event()

        async def speak(text: str) -> None:
            speaking.set()
            await asyncio.sleep(10)

        async def on_interrupt() -> None:
            raise ConnectionResetError("client went away mid-sentence")

        manager = TurnManager(
            session=SessionState(session_id="sess-voice", channel=SessionChannel.VOICE),
            orchestrator=None,  # type: ignore[arg-type]
            speak=speak,
            on_interrupt=on_interrupt,
        )
        manager.start()
        say = asyncio.create_task(manager._say("Something long."))
        await asyncio.wait_for(speaking.wait(), timeout=1)

        await manager.close(CloseReason.CALLER_HUNG_UP)
        await say
        assert manager.state is VoiceState.CLOSED


class TestPlayoutPacing:
    """Sending audio at the rate it is heard.

    Synthesis is roughly six times faster than speech: one slot offer measured
    18.8 seconds of audio delivered in 3.0. Unpaced, that overran the browser's
    playback buffer (heard as crackling) and left the server believing the
    agent had fallen silent fifteen seconds early — so it started the silence
    timer and asked "are you still there?" over its own voice.
    """

    @staticmethod
    def _chunk(seconds: float) -> bytes:
        from app.ai.providers.tts.groq import SAMPLE_RATE

        return b"\x00\x00" * int(SAMPLE_RATE * seconds)

    @pytest.mark.asyncio
    async def test_the_first_chunks_go_out_immediately(self) -> None:
        """Playback must start now, not after a lead-sized delay."""
        playout = voice_ws._Playout(24000)
        started = asyncio.get_running_loop().time()
        await playout.pace(self._chunk(1.0))
        assert asyncio.get_running_loop().time() - started < 0.05

    @pytest.mark.asyncio
    async def test_sending_is_held_back_once_it_runs_ahead(self) -> None:
        """Four seconds of audio must take about four seconds to play out.

        Sending finishes earlier than that by design — the pacer sleeps
        *before* each chunk, so the last one leaves the transport a lead ahead
        of the ear. What must hold is that sending plus draining matches real
        time, which is the property the silence timer depends on.
        """
        playout = voice_ws._Playout(24000)
        loop = asyncio.get_running_loop()
        started = loop.time()
        for _ in range(4):
            await playout.pace(self._chunk(1.0))
        sending = loop.time() - started
        await playout.drain()
        total = loop.time() - started

        assert sending > 0.5, f"sent 4s of audio in {sending:.2f}s -- not paced at all"
        assert sending <= 4.0 - voice_ws.PLAYOUT_LEAD_SECONDS + 0.3, (
            f"sending took {sending:.2f}s; the lead is not being used"
        )
        assert 3.7 <= total <= 4.6, f"4s of audio played out in {total:.2f}s"

    @pytest.mark.asyncio
    async def test_drain_waits_out_the_audio_still_in_the_buffer(self) -> None:
        """What stops the silence timer from starting mid-sentence."""
        playout = voice_ws._Playout(24000)
        await playout.pace(self._chunk(1.0))
        loop = asyncio.get_running_loop()
        started = loop.time()
        await playout.drain()
        waited = loop.time() - started
        assert 0.7 <= waited <= 1.3, f"drained in {waited:.2f}s, expected ~1s"

    @pytest.mark.asyncio
    async def test_drain_is_idle_when_nothing_was_sent(self) -> None:
        playout = voice_ws._Playout(24000)
        started = asyncio.get_running_loop().time()
        await playout.drain()
        assert asyncio.get_running_loop().time() - started < 0.05

    @pytest.mark.asyncio
    async def test_a_reset_utterance_does_not_inherit_the_old_cursor(self) -> None:
        """Barge-in resets the clock; otherwise the next reply is delayed by
        however much audio the caller just talked over."""
        playout = voice_ws._Playout(24000)
        await playout.pace(self._chunk(5.0))
        playout.reset()
        started = asyncio.get_running_loop().time()
        await playout.pace(self._chunk(1.0))
        assert asyncio.get_running_loop().time() - started < 0.05

    @pytest.mark.asyncio
    async def test_a_stalled_utterance_resumes_without_catching_up(self) -> None:
        """If playback has caught up, the cursor restarts from now.

        Otherwise a pause mid-utterance would leave the cursor in the past and
        the rest of the audio would be sent in one unpaced burst.
        """
        playout = voice_ws._Playout(24000)
        await playout.pace(self._chunk(0.1))
        await asyncio.sleep(0.3)  # playback overtakes the cursor
        started = asyncio.get_running_loop().time()
        await playout.pace(self._chunk(3.0))
        await playout.drain()
        waited = asyncio.get_running_loop().time() - started
        assert waited >= 2.5, f"burst {waited:.2f}s of a 3s utterance"
