"""Provider selection and per-call budget enforcement (Phase 12).

The factory is the only place a paid provider can be constructed, so it is the
only place that has to be right about whether one *should* be. These tests are
the ones that would fail if a configuration change quietly enabled spending.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.ai.providers.base import ChatMessage, LLMRequest, VoiceSpec
from app.ai.providers.factory import (
    GuardedLLMProvider,
    build_llm_provider,
    build_stt_provider,
    build_tts_provider,
)
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
