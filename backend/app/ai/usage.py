"""Provider usage metering and pricing.

Every provider adapter reports the units it consumed; this module prices them
and keeps the running ledger the budget guard reads. Vendor dashboards report
spend hours late, which makes them useless as a control -- so we meter locally
(ADR 006).

Figures are **estimates**. Vendor rounding and billing granularity mean the
local number will not match an invoice exactly; it needs to be conservative
enough to prevent an overrun, not exact.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final

from app.schemas.domain import ProviderUsageRecord

# Metric names ------------------------------------------------------------
INPUT_TOKENS: Final = "input_tokens"
OUTPUT_TOKENS: Final = "output_tokens"
REQUESTS: Final = "requests"
STT_SECONDS: Final = "stt_seconds"
TTS_CHARACTERS: Final = "tts_characters"
TTS_SECONDS: Final = "tts_seconds"

#: Unit cost in USD. Kept in one table so a rate change is a one-line edit.
#: Sources and date are recorded in COSTS.md; treat as approximate.
RATES: dict[tuple[str, str], Decimal] = {
    # OpenAI gpt-4.1-mini, per token (list price per 1M tokens / 1_000_000)
    ("openai", INPUT_TOKENS): Decimal("0.00000040"),
    ("openai", OUTPUT_TOKENS): Decimal("0.00000160"),
    ("openai", REQUESTS): Decimal("0"),
    # Deepgram streaming, per second of audio
    ("deepgram", STT_SECONDS): Decimal("0.00010000"),
    # ElevenLabs Flash, per character. Derived from the Starter plan -- $6/mo
    # for 30,000 credits, at Flash's 0.5 credits per character -- because
    # ElevenLabs sells a subscription, not usage. A prepaid bundle does not
    # meter cleanly, so this deliberately prices the *marginal* character as if
    # it were metered: the ledger's job is to stop an overrun, and a rate that
    # under-reports cannot. The earlier 0.00003 figure was list per-character
    # pricing from a plan this project does not use, and understated it ~3x.
    ("elevenlabs", TTS_CHARACTERS): Decimal("0.00010000"),
    ("elevenlabs", TTS_SECONDS): Decimal("0"),
}

#: Free providers still record usage, so the metering path is exercised by the
#: test suite rather than only in production.
#:
#: ``groq`` is here because its developer tier is rate-limited rather than
#: metered: tokens are counted, priced at zero, and the request budget that
#: actually binds is enforced by the vendor. Moving a Groq account to a paid
#: tier means moving it out of this set and into RATES, or the ledger will
#: under-report (COSTS.md).
FREE_PROVIDERS: frozenset[str] = frozenset({"mock", "groq", "ollama", "whisper", "piper"})


def price(provider: str, metric: str, quantity: Decimal | int | float) -> Decimal:
    """Estimated cost of ``quantity`` units. Unknown pairs cost nothing."""
    if provider in FREE_PROVIDERS:
        return Decimal("0")
    rate = RATES.get((provider, metric))
    if rate is None:
        return Decimal("0")
    return (rate * Decimal(str(quantity))).quantize(Decimal("0.000001"))


class UsageLedger:
    """In-memory ledger of metered usage.

    Phase 11 persists these rows to ``provider_usage``; the interface here is
    what the budget guard depends on, so persistence is a swap behind it.
    """

    def __init__(self, baseline_usd: Decimal | None = None) -> None:
        self._records: list[ProviderUsageRecord] = []
        self._lock = threading.Lock()
        # Spend from earlier runs, read back from ``provider_usage`` at startup.
        # Without it the ceiling resets every restart, which would make the
        # project budget unenforceable in exactly the situation it matters.
        self._baseline = baseline_usd or Decimal("0")

    def record(
        self,
        provider: str,
        metric: str,
        quantity: Decimal | int | float,
        session_id: str | None = None,
    ) -> ProviderUsageRecord:
        entry = ProviderUsageRecord(
            provider=provider,
            metric=metric,
            quantity=Decimal(str(quantity)),
            estimated_cost=price(provider, metric, quantity),
            session_id=session_id,
            recorded_at=datetime.now(UTC),
        )
        with self._lock:
            self._records.append(entry)
        return entry

    # ------------------------------------------------------------- queries
    @property
    def records(self) -> list[ProviderUsageRecord]:
        with self._lock:
            return list(self._records)

    def set_baseline(self, amount: Decimal) -> None:
        """Carry forward spend already recorded in the database."""
        with self._lock:
            self._baseline = amount

    @property
    def baseline(self) -> Decimal:
        return self._baseline

    def project_total(self) -> Decimal:
        """Everything spent on this project, across every run."""
        return self._baseline + sum((r.estimated_cost for r in self.records), Decimal("0"))

    def session_total(self, session_id: str) -> Decimal:
        return sum(
            (r.estimated_cost for r in self.records if r.session_id == session_id),
            Decimal("0"),
        )

    def session_quantity(self, session_id: str, metric: str) -> Decimal:
        return sum(
            (r.quantity for r in self.records if r.session_id == session_id and r.metric == metric),
            Decimal("0"),
        )

    def totals_by_provider(self) -> dict[str, Decimal]:
        totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        for record in self.records:
            totals[record.provider] += record.estimated_cost
        return dict(totals)

    def clear(self) -> None:
        with self._lock:
            self._records.clear()
            self._baseline = Decimal("0")


#: Process-wide ledger. Injected explicitly in tests.
_ledger = UsageLedger()


def get_usage_ledger() -> UsageLedger:
    return _ledger
