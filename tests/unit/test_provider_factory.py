"""Provider selection and per-call budget enforcement (Phase 12).

The factory is the only place a paid provider can be constructed, so it is the
only place that has to be right about whether one *should* be. These tests are
the ones that would fail if a configuration change quietly enabled spending.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.ai.budget_guard import BudgetGuard
from app.ai.providers.base import (
    ChatMessage,
    LLMRequest,
    ProviderUnavailableError,
    VoiceSpec,
)
from app.ai.providers.factory import (
    GuardedLLMProvider,
    GuardedTTSProvider,
    build_llm_provider,
    build_stt_provider,
    build_tts_provider,
)
from app.ai.providers.llm.mock import MockLLMProvider, MockTTSProvider
from app.ai.usage import OUTPUT_TOKENS, UsageLedger
from app.config.settings import Settings

ASK = LLMRequest(messages=[ChatMessage(role="user", content="hello")], session_id="sess-1")


def settings_for(**overrides: object) -> Settings:
    return Settings(_env_file=None, app_env="test", **overrides)  # type: ignore[arg-type]


class TestSelection:
    def test_the_default_configuration_builds_a_mock(self) -> None:
        provider = build_llm_provider(settings_for(), UsageLedger())
        assert provider.name == "mock"

    def test_mock_mode_cannot_build_a_paid_provider_even_with_a_key(self) -> None:
        """AI_MODE=mock is the single switch that guarantees zero spend."""
        provider = build_llm_provider(
            settings_for(ai_mode="mock", llm_provider="openai", openai_api_key="sk-real"),
            UsageLedger(),
        )
        assert provider.name == "mock"

    def test_selecting_openai_with_a_key_builds_the_paid_provider(self) -> None:
        provider = build_llm_provider(
            settings_for(ai_mode="cloud", llm_provider="openai", openai_api_key="sk-real"),
            UsageLedger(),
        )
        assert provider.name == "openai"
        assert isinstance(provider, GuardedLLMProvider)

    def test_a_paid_provider_is_always_wrapped_in_the_guard(self) -> None:
        """Unwrapped, a long session could run past the per-session ceiling."""
        provider = build_llm_provider(
            settings_for(ai_mode="cloud", llm_provider="openai", openai_api_key="sk-real"),
            UsageLedger(),
        )
        assert isinstance(provider, GuardedLLMProvider)

    def test_an_unimplemented_local_provider_falls_back_rather_than_pretending(self) -> None:
        provider = build_llm_provider(
            settings_for(ai_mode="local", llm_provider="ollama"), UsageLedger()
        )
        assert provider.name == "mock"

    def test_text_only_mode_builds_no_audio_providers(self) -> None:
        settings = settings_for(text_only_mode=True)
        assert build_stt_provider(settings, UsageLedger()) is None
        assert build_tts_provider(settings, UsageLedger()) is None

    def test_audio_providers_are_mocks_without_credentials(self) -> None:
        settings = settings_for(
            text_only_mode=False, voice_enabled=True, stt_enabled=True, tts_enabled=True
        )
        stt = build_stt_provider(settings, UsageLedger())
        tts = build_tts_provider(settings, UsageLedger())
        assert stt is not None and stt.name == "mock"
        assert tts is not None and tts.name == "mock"

    def test_paid_audio_providers_are_built_when_configured(self) -> None:
        settings = settings_for(
            ai_mode="cloud",
            text_only_mode=False,
            voice_enabled=True,
            stt_enabled=True,
            tts_enabled=True,
            stt_provider="deepgram",
            deepgram_api_key="dg-key",
            tts_provider="elevenlabs",
            elevenlabs_api_key="el-key",
            elevenlabs_voice_id="voice-1",
        )
        stt = build_stt_provider(settings, UsageLedger(), session_id="sess-1")
        tts = build_tts_provider(settings, UsageLedger(), session_id="sess-1")
        assert stt is not None and stt.name == "deepgram"
        assert tts is not None and tts.name == "elevenlabs"


class TestPerCallGuard:
    """The guard is consulted per call, not only when the provider is built."""

    def _guarded(self, ledger: UsageLedger, **overrides: object) -> GuardedLLMProvider:
        provider = build_llm_provider(
            settings_for(
                ai_mode="cloud",
                llm_provider="openai",
                openai_api_key="sk-real",
                **overrides,
            ),
            ledger,
        )
        assert isinstance(provider, GuardedLLMProvider)
        return provider

    async def test_a_call_within_budget_reaches_the_paid_provider(self) -> None:
        guarded = self._guarded(UsageLedger())
        assert guarded._choose("sess-1").name == "openai"

    async def test_the_project_ceiling_falls_back_mid_conversation(self) -> None:
        ledger = UsageLedger(baseline_usd=Decimal("20"))
        guarded = self._guarded(ledger)
        assert guarded._choose("sess-1").name == "mock"

    async def test_the_per_session_ceiling_falls_back(self) -> None:
        ledger = UsageLedger()
        guarded = self._guarded(ledger, max_estimated_session_cost_usd=Decimal("0.001"))
        # Spend past the session cap on this session only.
        ledger.record("openai", OUTPUT_TOKENS, 10_000, "sess-1")
        assert guarded._choose("sess-1").name == "mock"
        assert guarded._choose("sess-2").name == "openai"

    async def test_falling_back_still_answers_rather_than_raising(self) -> None:
        """Mid-conversation is the worst moment to discover a ceiling."""
        ledger = UsageLedger(baseline_usd=Decimal("20"))
        guarded = self._guarded(ledger)
        response = await guarded.generate(ASK)
        assert response.text == "hello"

    async def test_a_blocked_call_spends_nothing(self) -> None:
        ledger = UsageLedger(baseline_usd=Decimal("20"))
        guarded = self._guarded(ledger)
        before = ledger.project_total()
        await guarded.generate(ASK)
        assert ledger.project_total() == before


class TestNoPaidCallInTests:
    def test_the_default_test_settings_cannot_spend(self) -> None:
        settings = settings_for()
        assert settings.can_spend_money is False
        assert build_llm_provider(settings, UsageLedger()).name == "mock"

    async def test_a_mock_tts_stream_is_usable_without_credentials(self) -> None:
        settings = settings_for(text_only_mode=False, voice_enabled=True, tts_enabled=True)
        tts = build_tts_provider(settings, UsageLedger())
        assert tts is not None
        chunks = [c async for c in tts.synthesize_stream("hello there", VoiceSpec())]
        assert chunks


@pytest.mark.parametrize("provider", ["openai", "deepgram", "elevenlabs"])
def test_paid_providers_are_named_in_the_settings_allowlist(provider: str) -> None:
    from app.config.settings import PAID_PROVIDERS

    assert provider in PAID_PROVIDERS


class TestGroq:
    """Groq's free tier: rate-limited rather than metered."""

    def test_selecting_groq_with_a_key_builds_it(self) -> None:
        provider = build_llm_provider(
            settings_for(ai_mode="cloud", llm_provider="groq", groq_api_key="gsk-real"),
            UsageLedger(),
        )
        assert provider.name == "groq"

    def test_groq_is_not_treated_as_a_spending_provider(self) -> None:
        """It is free, so `can_spend_money` must stay false and the ceiling irrelevant."""
        settings = settings_for(ai_mode="cloud", llm_provider="groq", groq_api_key="gsk-real")
        assert settings.can_spend_money is False

    def test_a_full_ceiling_does_not_block_a_free_provider(self) -> None:
        """The fallback must always be reachable, and so must a free provider."""
        ledger = UsageLedger(baseline_usd=Decimal("20"))
        provider = build_llm_provider(
            settings_for(ai_mode="cloud", llm_provider="groq", groq_api_key="gsk-real"), ledger
        )
        assert isinstance(provider, GuardedLLMProvider)
        assert provider._choose("sess-1").name == "groq"

    def test_mock_mode_still_wins(self) -> None:
        provider = build_llm_provider(
            settings_for(ai_mode="mock", llm_provider="groq", groq_api_key="gsk-real"),
            UsageLedger(),
        )
        assert provider.name == "mock"

    def test_groq_without_a_key_falls_back_rather_than_crashing(self) -> None:
        provider = build_llm_provider(
            settings_for(ai_mode="cloud", llm_provider="mock"), UsageLedger()
        )
        assert provider.name == "mock"


class TestDegradingOnProviderFailure:
    """A rate limit or an outage must not end a call."""

    class Failing:
        name = "groq"

        def __init__(self) -> None:
            self.calls = 0

        async def generate(self, request: LLMRequest) -> object:
            self.calls += 1
            raise ProviderUnavailableError("groq rate limit reached (429)")

        async def tool_call(self, request: object) -> object:
            raise ProviderUnavailableError("groq rate limit reached (429)")

    async def test_a_provider_failure_falls_back_to_the_deterministic_path(self) -> None:
        failing = self.Failing()
        mock = MockLLMProvider()
        guarded = GuardedLLMProvider(
            failing,  # type: ignore[arg-type]
            mock,
            BudgetGuard(settings_for(), UsageLedger()),
        )
        response = await guarded.generate(ASK)
        assert response.text == "hello"
        assert failing.calls == 1

    async def test_a_failing_fallback_raises_rather_than_looping(self) -> None:
        failing = self.Failing()
        guarded = GuardedLLMProvider(
            failing,  # type: ignore[arg-type]
            failing,  # type: ignore[arg-type]
            BudgetGuard(settings_for(), UsageLedger()),
        )
        with pytest.raises(ProviderUnavailableError):
            await guarded.generate(ASK)


class TestDegradationIsAnnounced:
    """Falling back to silence must not be silent.

    The fallback emits silence-shaped bytes. Offline that is correct; on a live
    call it is the worst available behaviour, because the agent answers
    perfectly and the caller hears nothing -- indistinguishable from a crashed
    server, a dead microphone, or a broken socket. It cost three debugging
    sessions to recognise as a vendor rate limit, and a real caller would just
    hang up.
    """

    @staticmethod
    def _guarded(paid: object) -> GuardedTTSProvider:
        settings = Settings(_env_file=None, app_env="test")
        ledger = UsageLedger()
        return GuardedTTSProvider(
            paid,  # type: ignore[arg-type]
            MockTTSProvider(),
            BudgetGuard(settings, ledger),
            "sess-1",
        )

    @pytest.mark.asyncio
    async def test_a_vendor_failure_is_announced_and_still_answers(self) -> None:
        class Broken:
            name = "groq"

            async def synthesize_stream(self, text: str, voice: VoiceSpec):  # type: ignore[no-untyped-def]
                raise ProviderUnavailableError("Groq TTS returned 429: daily limit reached")
                yield b""  # pragma: no cover - never reached

        told: list[str] = []

        async def on_degraded(detail: str) -> None:
            told.append(detail)

        guarded = self._guarded(Broken())
        guarded.on_degraded = on_degraded
        audio = b"".join([c async for c in guarded.synthesize_stream("Hi.", VoiceSpec())])

        assert told, "degraded to silence without telling anyone"
        assert "429" in told[0], f"the notice does not say why: {told[0]!r}"
        assert audio, "the fallback should still produce frames"

    @pytest.mark.asyncio
    async def test_no_listener_is_not_an_error(self) -> None:
        """Text mode and the test suite attach nothing."""

        class Broken:
            name = "groq"

            async def synthesize_stream(self, text: str, voice: VoiceSpec):  # type: ignore[no-untyped-def]
                raise ProviderUnavailableError("down")
                yield b""  # pragma: no cover - never reached

        guarded = self._guarded(Broken())
        assert guarded.on_degraded is None
        assert b"".join([c async for c in guarded.synthesize_stream("Hi.", VoiceSpec())])

    @pytest.mark.asyncio
    async def test_a_failing_listener_does_not_break_the_call(self) -> None:
        """A socket that has already gone is the normal case here."""

        class Broken:
            name = "groq"

            async def synthesize_stream(self, text: str, voice: VoiceSpec):  # type: ignore[no-untyped-def]
                raise ProviderUnavailableError("down")
                yield b""  # pragma: no cover - never reached

        async def explode(detail: str) -> None:
            raise RuntimeError("socket closed")

        guarded = self._guarded(Broken())
        guarded.on_degraded = explode
        assert b"".join([c async for c in guarded.synthesize_stream("Hi.", VoiceSpec())])
