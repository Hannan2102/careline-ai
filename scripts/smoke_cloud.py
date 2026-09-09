#!/usr/bin/env python3
"""One live call to a cloud provider, deliberately hard to run by accident.

This is the only script in the repository that can spend money. It exists to
answer a question the test suite cannot: does the adapter work against the
real vendor? Everything else runs on mocks (ADR 005).

    python scripts/smoke_cloud.py --provider groq --confirm-spend
    python scripts/smoke_cloud.py --provider openai --confirm-spend

Refuses unless: the provider is explicitly named, ``--confirm-spend`` is
given, a key is configured, and the budget guard allows it. Prints the metered
cost afterwards so the number can be recorded in COSTS.md.

``--confirm-spend`` is required even for Groq's free tier. The flag means "I
intend to contact a vendor", which is the decision worth being deliberate
about; whether that particular call is billed is a detail of the plan.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
import time
import wave
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.agents.factory import open_database  # noqa: E402
from app.ai.budget_guard import BudgetGuard  # noqa: E402
from app.ai.providers.base import ChatMessage, LLMRequest, VoiceSpec  # noqa: E402
from app.ai.usage import get_usage_ledger  # noqa: E402
from app.config.settings import PAID_PROVIDERS, Settings, get_settings  # noqa: E402
from app.db.repositories import insert_usage  # noqa: E402
from app.observability.logging import configure_logging  # noqa: E402

BOLD, DIM, RED, GREEN, RESET = "\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[0m"

#: Kept tiny on purpose: the point is to prove the wiring, not to chat. The
#: whole exercise is budgeted under $0.25 in ROADMAP Phase 12.
PROMPT = "Reply with exactly: ok"
TTS_PHRASE = "Oakwood Family Medicine."


async def smoke_openai(settings: Settings) -> str:
    from app.ai.providers.llm.openai import OpenAILLMProvider

    provider = OpenAILLMProvider(
        api_key=settings.openai_api_key or "",
        model=settings.openai_model,
        ledger=get_usage_ledger(),
    )
    try:
        response = await provider.generate(
            LLMRequest(
                messages=[ChatMessage(role="user", content=PROMPT)],
                max_output_tokens=16,
                session_id="smoke",
            )
        )
        return f"model said {response.text.strip()!r} ({response.usage.output_tokens} tokens out)"
    finally:
        await provider.aclose()


async def smoke_groq(settings: Settings) -> str:
    """Groq speaks the OpenAI wire format, so the same adapter serves it."""
    from app.ai.providers.llm.openai import OpenAILLMProvider

    provider = OpenAILLMProvider(
        api_key=settings.groq_api_key or "",
        model=settings.groq_model,
        base_url=settings.groq_base_url,
        ledger=get_usage_ledger(),
        name="groq",
        supports_message_name=False,
        reasoning_effort=settings.groq_reasoning_effort,
    )
    try:
        response = await provider.generate(
            LLMRequest(
                messages=[ChatMessage(role="user", content=PROMPT)],
                # Generous enough that a reasoning model still has tokens left
                # for an actual answer after it has finished thinking.
                max_output_tokens=128,
                session_id="smoke",
            )
        )
        return (
            f"{settings.groq_model} said {response.text.strip()!r} "
            f"({response.usage.input_tokens} in, {response.usage.output_tokens} out)"
        )
    finally:
        await provider.aclose()


async def smoke_elevenlabs(settings: Settings) -> str:
    from app.ai.providers.tts.elevenlabs import ElevenLabsTTSProvider

    provider = ElevenLabsTTSProvider(
        api_key=settings.elevenlabs_api_key or "",
        voice_id=settings.elevenlabs_voice_id or "",
        model=settings.elevenlabs_model,
        ledger=get_usage_ledger(),
        session_id="smoke",
    )
    try:
        total = 0
        async for chunk in provider.synthesize_stream(TTS_PHRASE, VoiceSpec()):
            total += len(chunk)
        return f"received {total} bytes of audio for {len(TTS_PHRASE)} characters"
    finally:
        await provider.aclose()


async def smoke_deepgram(settings: Settings) -> str:
    """Stream real speech at Deepgram and check it comes back as words.

    The audio is synthesised locally by macOS `say`, which makes this
    repeatable and free of any recording. Transcribing *silence* would prove
    only that the socket opened -- and would still be billed -- so the check is
    that a known sentence survives the round trip.

    Streamed in 100 ms chunks with real pauses between them, because that is
    the property being tested: Deepgram finalises while the speaker is still
    talking, and a burst upload would not exercise that at all.
    """
    from app.ai.providers.stt.deepgram import DeepgramSTTProvider

    spoken = "I would like to book an appointment with doctor Patel on Thursday."
    pcm = await _synthesise_locally(spoken)
    if pcm is None:
        return "skipped - no local speech synthesiser to generate test audio"

    ledger = get_usage_ledger()
    stt = DeepgramSTTProvider(
        api_key=settings.deepgram_api_key or "",
        model=settings.deepgram_model,
        ledger=ledger,
        session_id="smoke",
    )

    chunk = 3200  # 100 ms of 16 kHz 16-bit mono

    async def audio() -> AsyncIterator[bytes]:
        for start in range(0, len(pcm), chunk):
            yield pcm[start : start + chunk]
            await asyncio.sleep(0.1)

    started = time.perf_counter()
    finals: list[str] = []
    interims = 0
    first_interim_at: float | None = None
    async for transcript in stt.transcribe_stream(audio()):
        if transcript.is_final:
            finals.append(transcript.text)
        else:
            interims += 1
            if first_interim_at is None:
                first_interim_at = time.perf_counter()

    heard = " ".join(finals).strip()
    if not heard:
        return "connected, but Deepgram returned no transcript"

    audio_seconds = len(pcm) / (16000 * 2)
    print(f"    {DIM}said:  {spoken}{RESET}")
    print(f"    {DIM}heard: {heard}{RESET}")
    if first_interim_at is not None:
        # The number that justifies streaming over batch: a partial result
        # while the speaker is still talking (COSTS.md).
        print(
            f"    {DIM}first interim after {(first_interim_at - started) * 1000:.0f} ms "
            f"of a {audio_seconds:.1f}s utterance, {interims} in total{RESET}"
        )
    return f"transcribed {audio_seconds:.1f}s of speech"


async def _synthesise_locally(text: str) -> bytes | None:
    """16 kHz mono 16-bit PCM from the OS, or ``None`` if it cannot.

    Only macOS is handled. This is a developer smoke test, not something CI
    runs, so a missing synthesiser is a skip rather than a failure.
    """
    if sys.platform != "darwin":
        return None
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "utterance.wav"
        process = await asyncio.create_subprocess_exec(
            "say",
            "--data-format=LEI16@16000",
            "-o",
            str(path),
            text,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        if await process.wait() != 0 or not path.exists():
            return None
        with wave.open(str(path), "rb") as handle:
            return handle.readframes(handle.getnframes())


SMOKES = {
    "groq": (smoke_groq, "groq_api_key"),
    "openai": (smoke_openai, "openai_api_key"),
    "elevenlabs": (smoke_elevenlabs, "elevenlabs_api_key"),
    "deepgram": (smoke_deepgram, "deepgram_api_key"),
}


async def main() -> int:
    parser = argparse.ArgumentParser(description="One live paid call. Costs money.")
    parser.add_argument("--provider", choices=sorted(SMOKES), required=True)
    parser.add_argument(
        "--confirm-spend",
        action="store_true",
        help="Required. Without it this script refuses to call anything.",
    )
    args = parser.parse_args()
    configure_logging(level="ERROR", json_output=False)

    settings = get_settings()
    smoke, key_field = SMOKES[args.provider]

    if not args.confirm_spend:
        print(f"{RED}Refusing: --confirm-spend not given. This contacts a real vendor.{RESET}")
        return 2

    if not getattr(settings, key_field):
        print(f"{RED}Refusing: {key_field.upper()} is not set.{RESET}")
        return 2

    # Spend already recorded by previous runs counts against the ceiling.
    database = await open_database(settings)
    ledger = get_usage_ledger()
    guard = BudgetGuard(settings, ledger)
    decision = guard.check(args.provider, "smoke")
    before = ledger.project_total()

    free = args.provider not in PAID_PROVIDERS
    print(f"{BOLD}Smoke test: {args.provider}{RESET}")
    if free:
        print(f"{DIM}Free tier: metered but not billed. Rate limits apply.{RESET}")
    print(f"{DIM}Spent so far: ${before:.4f} of ${settings.max_estimated_project_cost_usd}{RESET}")

    if not decision.allowed:
        print(f"{RED}Refusing: {decision.reason}{RESET}")
        if database is not None:
            await database.dispose()
        return 2

    try:
        outcome = await smoke(settings)
    except Exception as exc:
        print(f"{RED}Failed: {exc}{RESET}")
        if database is not None:
            await database.dispose()
        return 1

    spent = ledger.project_total() - before
    print(f"{GREEN}OK{RESET} - {outcome}")
    print(f"{BOLD}This call cost an estimated ${spent:.6f}{RESET}")
    print(f"{DIM}Project total now ${ledger.project_total():.4f}. Record it in COSTS.md.{RESET}")

    # Persist the usage, or the next run would not know this one happened.
    if database is not None:
        new_records = [r for r in ledger.records if r.session_id == "smoke"]
        if new_records:
            async with database.session() as db:
                await insert_usage(db, new_records)
        await database.dispose()

    # ROADMAP Phase 12 budgets this exercise at under $0.25. Exceeding that is
    # not a failure of the adapter, but it is worth noticing loudly.
    if spent >= Decimal("0.25"):
        print(f"{RED}That single call cost ${spent:.4f}, over the $0.25 smoke budget.{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
