#!/usr/bin/env python3
"""Talk to the agent from the terminal.

Text mode: no speech, no paid API call, the same orchestrator and workflows
voice will use (ADR 005).

    python scripts/text_chat.py
    python scripts/text_chat.py --trace          # show the per-turn trace
    python scripts/text_chat.py --script demo1   # replay a scripted demo

Everything it touches is synthetic (synthetic-data/README.md).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.agents.factory import build_runtime, open_database  # noqa: E402
from app.agents.state import SessionChannel  # noqa: E402
from app.agents.trace import TurnTrace  # noqa: E402
from app.config.settings import EHRProviderName, get_settings  # noqa: E402
from app.ehr.factory import get_default_memory_store  # noqa: E402
from app.ehr.seeding import seed_memory_store  # noqa: E402
from app.observability.logging import configure_logging  # noqa: E402

BOLD, DIM, CYAN, YELLOW, RESET = "\033[1m", "\033[2m", "\033[36m", "\033[33m", "\033[0m"

#: Reproducible demos (DEMO.md). Handy for a walkthrough, and for checking a
#: change did not alter a conversation you already know the shape of.
SCRIPTS: dict[str, list[str]] = {
    "demo1": [
        "Hi, I'd like to schedule a diabetes follow-up with Dr. Patel next week",
        "My name is John Smith and I was born 15 February 1985",
        "The first one please",
        "Yes",
    ],
    "demo2": [
        "I forgot how much Metformin I'm supposed to take",
        "I'm John Smith, date of birth 1985-02-15",
    ],
    "demo3": [
        "My blood pressure medicine makes me dizzy. Should I take half?",
    ],
    "demo5": ["Are you open on Saturday?"],
    "demo6": [
        "When is my appointment?",
        "I'm Jane Doe, born 1 January 1970",
        "Jane Doe, 01/01/1970",
        "Jane Doe, born 1970-01-01",
    ],
    "refill": [
        "I need a refill on my metformin",
        "My name is John Smith, born 15 February 1985",
        "Yes please",
    ],
}


def show_trace(trace: TurnTrace) -> None:
    print(f"{DIM}    intent      {trace.intent.value} ({trace.confidence:.2f})")
    print(
        f"    safety      {trace.safety_outcome.value}"
        + (f" [{trace.safety_rule}]" if trace.safety_rule else "")
    )
    print(f"    workflow    {trace.workflow or '-'} / {trace.workflow_state or '-'}")
    print(f"    entities    {trace.entities or '{}'}")
    if trace.operations:
        print(f"    operations  {', '.join(o.action.value for o in trace.operations)}")
    if trace.escalation_id:
        print(f"    escalation  {trace.escalation_id}")
    print(f"    verification {trace.verification_state}")
    print(
        f"    latency     safety {trace.timings.safety_ms:.1f}ms | "
        f"extract {trace.timings.extraction_ms:.1f}ms | "
        f"workflow {trace.timings.workflow_ms:.1f}ms | "
        f"total {trace.timings.total_ms:.1f}ms"
    )
    print(f"    est. cost   ${float(trace.estimated_cost_usd):.4f}{RESET}")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Text-mode conversation with the agent.")
    parser.add_argument("--trace", action="store_true", help="Show the per-turn trace.")
    parser.add_argument("--script", choices=sorted(SCRIPTS), help="Replay a scripted demo.")
    parser.add_argument(
        "--verbose", action="store_true", help="Show INFO logs alongside the conversation."
    )
    args = parser.parse_args()

    # A conversation is unreadable interleaved with structured logs. The
    # trace is the readable view; --verbose brings the logs back.
    configure_logging(level="INFO" if args.verbose else "ERROR", json_output=False)

    settings = get_settings()
    if settings.ehr_provider is EHRProviderName.MEMORY:
        await seed_memory_store(get_default_memory_store())

    # The same database the API uses, so a conversation held here shows up on
    # the dashboard. A CLI that recorded nothing would make `make chat` a
    # different product from the one the dashboard describes.
    database = await open_database(settings)
    runtime = build_runtime(settings=settings, database=database)
    session = runtime.sessions.create(channel=SessionChannel.TEXT)

    print(f"{BOLD}CareLine AI — Oakwood Family Medicine{RESET}")
    print(
        f"{DIM}Synthetic data only. EHR: {runtime.ehr.name} | "
        f"AI mode: {settings.ai_mode.value} | session: {session.session_id}{RESET}"
    )
    print(f"{DIM}Type 'quit' to end.{RESET}\n")

    utterances = SCRIPTS[args.script] if args.script else None
    index = 0

    while session.is_active:
        if utterances is not None:
            if index >= len(utterances):
                break
            utterance = utterances[index]
            index += 1
            print(f"{CYAN}You:{RESET} {utterance}")
        else:
            try:
                utterance = input(f"{CYAN}You:{RESET} ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not utterance:
                continue
            if utterance.lower() in {"quit", "exit", "bye"}:
                break

        result = await runtime.orchestrator.handle_turn(session, utterance)
        print(f"{YELLOW}Agent:{RESET} {result.message}\n")
        if args.trace:
            show_trace(result.trace)
            print()

    await runtime.end_session(session.session_id)
    escalations = runtime.escalations.store.for_session(session.session_id)
    if escalations:
        print(f"{DIM}Escalations raised: {', '.join(e.category.value for e in escalations)}{RESET}")
    print(
        f"{DIM}Estimated cost this session: "
        f"${float(runtime.ledger.session_total(session.session_id)):.4f}{RESET}"
    )
    if database is not None:
        print(f"{DIM}Recorded. Open the dashboard's Agent Trace to inspect it.{RESET}")
        await database.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
