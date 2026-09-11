"""Time to first audio, measured against the live speech provider.

Phase 14 requires every optimisation to cite a before and an after from real
measurements (docs/latency.md), and this is what produces them. Synthesis is
the only stage of a turn still outside its budget, so it is the only stage
worth optimising, and that claim needs a number rather than an intuition.

What is measured is **time to the first byte of audio** -- not total synthesis
time. A caller hears the gap before the agent starts talking; what happens
after that is covered by the fact that speech is slower than synthesis.

    make measure-tts            # the configured provider
    make measure-tts ARGS=--rest   # force the REST path, for a comparison

Costs a few cents of Deepgram credit: roughly 700 characters per run.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time

import httpx

from app.ai.providers.base import VoiceSpec
from app.ai.providers.factory import build_tts_provider
from app.ai.providers.tts.deepgram import DeepgramTTSProvider
from app.ai.usage import get_usage_ledger
from app.config.settings import get_settings

#: Real agent replies, at the lengths the agent really produces them. A short
#: line and a long one have the same time to first audio if the provider is
#: streaming, and very different times if it is not -- which is the whole
#: question, so both are here.
UTTERANCES = (
    "Could I take your full name and date of birth?",
    "Thank you, I've found you. What would you like to be seen about?",
    "That's Thursday 10 September at 8:00 AM with Dr. Emily Chen. Shall I book that for you?",
    "I have these times for a follow up: 1) Wednesday 9 September at 8:00 AM with "
    "Dr. Emily Chen; 2) Thursday 10 September at 8:00 AM with Dr. Michael Johnson; "
    "3) Friday 11 September at 8:00 AM with Dr. Sarah Patel. Which of those works best?",
)


async def first_audio_ms(provider: object, text: str) -> float:
    """Time to the first byte, then read the rest and let it finish cleanly.

    Draining matters. Stopping at the first chunk is what a barge-in does, and
    a provider holding a socket open across the call then has an interrupted
    utterance queued on it -- so a measurement that abandoned every stream
    would be measuring the reconnect path, not the one a caller gets.
    """
    started = time.perf_counter()
    first: float | None = None
    async for chunk in provider.synthesize_stream(text, VoiceSpec()):  # type: ignore[attr-defined]
        if chunk and first is None:
            first = (time.perf_counter() - started) * 1000
    if first is None:
        raise RuntimeError(f"no audio came back for {text!r}")
    return first


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rest", action="store_true", help="force the REST path")
    parser.add_argument("--ab", action="store_true", help="both paths, interleaved")
    parser.add_argument("--runs", type=int, default=3, help="passes over the utterances")
    parser.add_argument(
        "--gap",
        type=float,
        default=0.0,
        help="seconds between utterances, as a real call has (try 7)",
    )
    args = parser.parse_args()

    if args.ab:
        return await compare(args.runs, args.gap)

    settings = get_settings()
    ledger = get_usage_ledger()
    if args.rest:
        provider: object = DeepgramTTSProvider(
            api_key=settings.deepgram_api_key or "",
            ledger=ledger,
            session_id="measure",
            streaming=False,
        )
    else:
        provider = build_tts_provider(settings, ledger, "measure")
    if provider is None:
        print("No speech provider is configured. Set TTS_ENABLED=true.", file=sys.stderr)
        return 1

    name = getattr(provider, "name", "?")
    path = "REST" if args.rest else "websocket"
    print(f"{name} · {path} · {args.runs} runs over {len(UTTERANCES)} utterances\n")

    timings: list[float] = []
    for run in range(1, args.runs + 1):
        for utterance in UTTERANCES:
            ms = await first_audio_ms(provider, utterance)
            timings.append(ms)
            print(f"  run {run}  {ms:7.0f} ms  {len(utterance):>4} chars  {utterance[:48]}...")

    # The first utterance of a call pays for opening the socket; every one
    # after it does not. Reported separately because averaging them together
    # describes a call nobody has.
    print(f"\n  first utterance {timings[0]:.0f} ms  (includes opening the connection)")
    rest = timings[1:]
    if rest:
        print(
            f"  after that      median {statistics.median(rest):.0f} ms"
            f" · min {min(rest):.0f} · max {max(rest):.0f}  (n={len(rest)})"
        )
    print(
        f"\n  overall median {statistics.median(timings):.0f} ms"
        f" · mean {statistics.fmean(timings):.0f} ms"
        f" · min {min(timings):.0f} ms · max {max(timings):.0f} ms"
        f"  (n={len(timings)})"
    )
    closer = getattr(provider, "aclose", None)
    if closer is not None:
        await closer()
    return 0


async def compare(runs: int, gap: float = 0.0) -> int:
    """Every path, alternating, so one network answers all the questions.

    Taken minutes apart the comparison is worthless: REST measured 296 ms in
    one session and 166 ms in the next, with a 4.5-second outlier in between.
    Interleaving is the only way a difference means anything — whatever the
    line is doing, it is doing it to all of them.

    ``gap`` is the other half of being honest. Back to back, REST looks as
    fast as the socket, because httpx is holding a connection open between
    requests. A real caller speaks for several seconds between the agent's
    replies, and httpx expires an idle connection after five — so a benchmark
    with no gap measures a warm pool that a real call never has. Pass
    ``--gap 7`` to measure the thing being shipped.
    """
    settings = get_settings()
    ledger = get_usage_ledger()
    key = settings.deepgram_api_key or ""
    if not key:
        print("Set DEEPGRAM_API_KEY to compare.", file=sys.stderr)
        return 1

    def rest_provider(keepalive: float) -> DeepgramTTSProvider:
        client = httpx.AsyncClient(
            base_url="https://api.deepgram.com",
            timeout=30.0,
            limits=httpx.Limits(max_connections=10, keepalive_expiry=keepalive),
        )
        return DeepgramTTSProvider(
            api_key=key, ledger=ledger, session_id="measure", streaming=False, client=client
        )

    paths: list[tuple[str, DeepgramTTSProvider]] = [
        ("REST", rest_provider(5.0)),
        ("REST keepalive", rest_provider(120.0)),
        (
            "websocket",
            DeepgramTTSProvider(api_key=key, ledger=ledger, session_id="measure", streaming=True),
        ),
    ]
    results: dict[str, list[float]] = {label: [] for label, _ in paths}
    print(f"interleaved · {runs} runs over {len(UTTERANCES)} utterances · {gap:.0f}s gap\n")
    for run in range(1, runs + 1):
        for utterance in UTTERANCES:
            if gap:
                await asyncio.sleep(gap)
            for label, provider in paths:
                results[label].append(await first_audio_ms(provider, utterance))
            line = "   ".join(f"{label} {results[label][-1]:6.0f} ms" for label, _ in paths)
            print(f"  run {run}  {line}   {len(utterance):>4} chars")

    print()
    for label, timings in results.items():
        # The first of each is the connection being opened, and only the
        # websocket keeps one, so it is reported apart from the steady state.
        rest_of = timings[1:]
        print(
            f"  {label:<10} first {timings[0]:6.0f} ms · then median "
            f"{statistics.median(rest_of):.0f} ms (n={len(rest_of)})"
        )
    saved = statistics.median(results["REST"][1:]) - statistics.median(results["websocket"][1:])
    print(f"\n  websocket saves {saved:.0f} ms per utterance against plain REST")

    for _, provider in paths:
        await provider.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
