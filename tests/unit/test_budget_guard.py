"""Budget guard behaviour at every threshold (ADR 006, DEMO.md scenario 9)."""

from __future__ import annotations

from decimal import Decimal

from app.ai.budget_guard import BudgetGuard
from app.ai.usage import (
    INPUT_TOKENS,
    OUTPUT_TOKENS,
    RATES,
    STT_SECONDS,
    TTS_CHARACTERS,
    UsageLedger,
    price,
)
from app.config.settings import Settings


def _settings(**overrides: object) -> Settings:
    overrides.setdefault("app_env", "test")
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def spend(ledger: UsageLedger, amount: str, session_id: str | None = None) -> None:
    """Record synthetic OpenAI usage costing exactly ``amount`` dollars.

    Derived from the raw rate rather than from ``price()``, whose per-record
    rounding would distort a single-unit division.
    """
    tokens = int(Decimal(amount) / RATES[("openai", OUTPUT_TOKENS)])
    ledger.record("openai", OUTPUT_TOKENS, tokens, session_id=session_id)


class TestProjectThresholds:
    def test_fresh_ledger_allows_paid_calls(self, guard: BudgetGuard) -> None:
        decision = guard.check("openai")
        assert (decision.allowed, decision.status) == (True, "ok")
        assert decision.remaining_usd == Decimal("20")

    def test_just_below_warn_is_still_ok(self, guard: BudgetGuard, ledger: UsageLedger) -> None:
        spend(ledger, "14.99")
        decision = guard.check("openai")
        assert (decision.allowed, decision.status) == (True, "ok")

    def test_reaching_warn_warns_but_still_allows(
        self, guard: BudgetGuard, ledger: UsageLedger
    ) -> None:
        """DEMO scenario 8: a warning appears; paid providers remain available."""
        spend(ledger, "15.00")
        decision = guard.check("openai")
        assert (decision.allowed, decision.status) == (True, "warn")
        assert decision.reason is not None and "warning threshold" in decision.reason

    def test_just_below_ceiling_still_allows(self, guard: BudgetGuard, ledger: UsageLedger) -> None:
        spend(ledger, "19.99")
        assert guard.check("openai").allowed is True

    def test_reaching_the_ceiling_blocks_paid_calls(
        self, guard: BudgetGuard, ledger: UsageLedger
    ) -> None:
        """DEMO scenario 9: optional paid calls are blocked at the ceiling."""
        spend(ledger, "20.00")
        decision = guard.check("openai")
        assert (decision.allowed, decision.status) == (False, "blocked")
        assert decision.reason is not None and "mock providers" in decision.reason

    def test_free_providers_are_never_blocked(
        self, guard: BudgetGuard, ledger: UsageLedger
    ) -> None:
        """The fallback must stay reachable, otherwise blocking would break the system."""
        spend(ledger, "100.00")
        for provider in ("mock", "ollama", "whisper", "piper"):
            decision = guard.check(provider)
            assert decision.allowed is True, provider


class TestSessionCeilings:
    def test_session_cost_cap_trips_independently_of_the_project_total(
        self, ledger: UsageLedger
    ) -> None:
        """One runaway session must not be able to consume the project budget."""
        guard = BudgetGuard(_settings(max_estimated_session_cost_usd=Decimal("1")), ledger)
        spend(ledger, "1.00", session_id="sess-1")

        assert guard.check("openai", session_id="sess-1").allowed is False
        assert guard.check("openai", session_id="sess-2").allowed is True
        assert guard.check("openai").allowed is True  # project total is only $1

    def test_tts_character_cap_blocks_further_synthesis(self, ledger: UsageLedger) -> None:
        guard = BudgetGuard(_settings(max_tts_characters_per_session=3000), ledger)
        ledger.record("elevenlabs", TTS_CHARACTERS, 3000, session_id="sess-1")
        decision = guard.check("elevenlabs", session_id="sess-1")
        assert decision.allowed is False
        assert decision.reason is not None and "TTS character cap" in decision.reason

    def test_stt_minute_cap_blocks_further_recognition(self, ledger: UsageLedger) -> None:
        guard = BudgetGuard(_settings(max_stt_minutes_per_session=10), ledger)
        ledger.record("deepgram", STT_SECONDS, 600, session_id="sess-1")
        decision = guard.check("deepgram", session_id="sess-1")
        assert decision.allowed is False
        assert decision.reason is not None and "STT minute cap" in decision.reason


class TestOverride:
    def test_override_allows_spending_in_development(self, ledger: UsageLedger) -> None:
        guard = BudgetGuard(_settings(app_env="development", budget_guard_override=True), ledger)
        spend(ledger, "50.00")
        decision = guard.check("openai")
        assert decision.allowed is True
        assert decision.status == "warn"

    def test_override_is_ignored_in_production(self, ledger: UsageLedger) -> None:
        """The escape hatch is for a supervised demo, never for a deployed system."""
        guard = BudgetGuard(_settings(app_env="production", budget_guard_override=True), ledger)
        spend(ledger, "50.00")
        assert guard.check("openai").allowed is False


class TestMetering:
    def test_mock_usage_is_recorded_but_free(self, ledger: UsageLedger) -> None:
        """Free providers still exercise the metering path, so it is never untested."""
        record = ledger.record("mock", OUTPUT_TOKENS, 100_000, session_id="s")
        assert record.estimated_cost == Decimal("0")
        assert len(ledger.records) == 1
        assert ledger.project_total() == Decimal("0")

    def test_costs_accumulate_per_provider_and_per_session(self, ledger: UsageLedger) -> None:
        ledger.record("openai", INPUT_TOKENS, 1_000_000, session_id="s1")
        ledger.record("elevenlabs", TTS_CHARACTERS, 1000, session_id="s1")
        ledger.record("openai", INPUT_TOKENS, 1_000_000, session_id="s2")

        assert ledger.session_total("s1") == Decimal("0.430000")
        assert ledger.totals_by_provider()["openai"] == Decimal("0.800000")
        assert ledger.project_total() == Decimal("0.830000")

    def test_unknown_metrics_cost_nothing_rather_than_raising(self) -> None:
        assert price("openai", "unmetered_thing", 10) == Decimal("0")
        assert price("brand-new-vendor", INPUT_TOKENS, 10) == Decimal("0")

    def test_summary_reports_the_state_the_dashboard_shows(
        self, guard: BudgetGuard, ledger: UsageLedger
    ) -> None:
        spend(ledger, "16.00")
        summary = guard.summary()
        assert summary["status"] == "warn"
        assert summary["paid_calls_allowed"] is True
        assert summary["project_limit_usd"] == "20"


class TestSpendSurvivesRestart:
    """A ceiling that resets on restart is not a ceiling (Phase 11)."""

    def test_a_baseline_counts_toward_the_project_total(self, ledger: UsageLedger) -> None:
        ledger.set_baseline(Decimal("12.00"))
        spend(ledger, "3.00")
        assert ledger.project_total() == Decimal("15.00")

    def test_carried_forward_spend_can_trip_the_warning(
        self, ledger: UsageLedger, guard: BudgetGuard
    ) -> None:
        ledger.set_baseline(Decimal("15.50"))
        decision = guard.check("openai")
        assert decision.status == "warn"
        assert decision.allowed is True

    def test_carried_forward_spend_can_block(self, ledger: UsageLedger, guard: BudgetGuard) -> None:
        """A fresh process with $20 already spent must not start spending again."""
        ledger.set_baseline(Decimal("20.00"))
        assert ledger.records == []  # nothing spent in *this* run
        decision = guard.check("openai")
        assert decision.allowed is False
        assert decision.status == "blocked"

    def test_session_totals_ignore_the_baseline(self, ledger: UsageLedger) -> None:
        """Per-session caps are about this call, not the project's history."""
        ledger.set_baseline(Decimal("19.00"))
        spend(ledger, "0.25", session_id="s1")
        assert ledger.session_total("s1") == Decimal("0.25")

    def test_clearing_resets_the_baseline_too(self, ledger: UsageLedger) -> None:
        ledger.set_baseline(Decimal("10.00"))
        ledger.clear()
        assert ledger.project_total() == Decimal("0")
