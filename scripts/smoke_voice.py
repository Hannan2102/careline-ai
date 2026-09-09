"""Drive one whole voice call against the live providers and save the audio.

The header stripper is why this exists. A byte-offset error in it does not
raise -- it turns speech into static, which no assertion in the test suite
would catch and no mocked test could reproduce, because the mock produces the
container the stripper expects. The only way to know is to run it against what
the vendor actually sends and listen.

Speech recognition is scripted rather than live: this checks the *output* half
of the pipeline plus the agent, and a microphone would make it unrepeatable.

    make smoke-voice

Costs nothing: Groq's free tier serves both the model and the speech.
"""

from __future__ import annotations

import asyncio
import struct
import sys
import time
import wave
from collections.abc import AsyncIterator
from pathlib import Path

from app.agents.factory import build_runtime, open_database
from app.agents.state import SessionChannel
from app.ai.budget_guard import BudgetGuard
from app.ai.providers.base import Transcript
from app.ai.providers.factory import build_tts_provider
from app.ai.providers.tts.groq import SAMPLE_RATE
from app.ai.usage import get_usage_ledger
from app.config.settings import get_settings
from app.voice.session import VoiceSession
from app.voice.turn_manager import TurnTimings

OUT = Path("smoke_voice.wav")

SCRIPT = [
    "Hi, I'd like to book an appointment",
    "My name is John Smith",
    "I was born on the fourteenth of March nineteen seventy eight",
]


class ScriptedSTT:
    """Feeds the scripted utterances, one per chunk of inbound audio."""

    name = "mock"

    def __init__(self, utterances: list[str]) -> None:
        self.utterances = utterances

    async def transcribe_stream(self, audio: AsyncIterator[bytes]) -> AsyncIterator[Transcript]:
        index = 0
        async for _chunk in audio:
            if index >= len(self.utterances):
                return
            print(f"  caller > {self.utterances[index]}")
            yield Transcript(
                text=self.utterances[index], is_final=True, confidence=1.0, audio_seconds=1.0
            )
            index += 1


async def paced_audio(count: int) -> AsyncIterator[bytes]:
    """One frame per scripted utterance, spaced so turns actually complete."""
    for _ in range(count):
        yield b"\x00" * 640
        await asyncio.sleep(1.2)
    await asyncio.sleep(2.0)


async def main() -> int:
    settings = get_settings()
    if settings.tts_provider != "groq":
        print("Set TTS_PROVIDER=groq and TTS_ENABLED=true (and TEXT_ONLY_MODE=false).")
        return 2

    database = await open_database(settings)
    runtime = build_runtime(settings=settings, database=database)
    ledger = get_usage_ledger()
    session = runtime.sessions.create(channel=SessionChannel.VOICE)

    tts = build_tts_provider(settings, ledger, session.session_id)
    assert tts is not None

    captured = bytearray()
    first_audio_at: float | None = None
    started = time.perf_counter()

    async def audio_out(chunk: bytes) -> None:
        nonlocal first_audio_at
        if first_audio_at is None:
            first_audio_at = time.perf_counter()
        captured.extend(chunk)

    voice = VoiceSession(
        session=session,
        orchestrator=runtime.orchestrator,
        stt=ScriptedSTT(SCRIPT),
        tts=tts,
        audio_out=audio_out,
        guard=BudgetGuard(settings, ledger),
        timings=TurnTimings(end_of_utterance=__import__("datetime").timedelta(milliseconds=400)),
    )

    print(f"tts={tts.name}  llm={settings.llm_provider}\n")
    await voice.run(paced_audio(len(SCRIPT)))
    await runtime.end_session(session.session_id)

    if not captured:
        print("\nNo audio was produced.")
        return 1

    with wave.open(str(OUT), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(bytes(captured))

    seconds = len(captured) / (SAMPLE_RATE * 2)
    # A stripper that lost sync produces noise, and noise is loud: a sane
    # utterance averages well under a third of full scale.
    samples = struct.unpack(f"<{len(captured) // 2}h", bytes(captured[: len(captured) // 2 * 2]))
    level = sum(abs(s) for s in samples) / len(samples) / 32768

    print(f"\n  turns          {voice.manager.stats.turns}")
    print(f"  audio          {seconds:.2f}s -> {OUT}")
    print(f"  first audio    {(first_audio_at - started) * 1000:.0f} ms after the call started")
    print(f"  mean level     {level:.3f}  ({'plausible speech' if level < 0.30 else 'NOISE'})")
    print(f"  estimated cost ${ledger.project_total():.4f}")
    if database is not None:
        await database.dispose()
    return 0 if level < 0.30 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
