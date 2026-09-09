"""Configuration rules: the switches that make overspending impossible."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config.settings import AIMode, Settings


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def test_defaults_cannot_spend_money() -> None:
    s = _settings()
    assert s.ai_mode is AIMode.MOCK
    assert s.text_only_mode is True
    assert s.can_spend_money is False
    assert s.selected_paid_providers == set()


def test_mock_mode_overrides_individual_provider_choices() -> None:
    """AI_MODE=mock is a single switch that guarantees zero spend."""
    s = _settings(ai_mode="mock", llm_provider="openai", stt_provider="deepgram")
    assert (s.llm_provider, s.stt_provider, s.tts_provider) == ("mock", "mock", "mock")
    assert s.can_spend_money is False


def test_text_only_mode_disables_audio() -> None:
    s = _settings(text_only_mode=True, voice_enabled=True, stt_enabled=True, tts_enabled=True)
    assert (s.voice_enabled, s.stt_enabled, s.tts_enabled) == (False, False, False)


def test_paid_provider_without_key_fails_fast() -> None:
    with pytest.raises(ValidationError, match="OPENAI_API_KEY"):
        _settings(ai_mode="cloud", llm_provider="openai", openai_api_key=None)


def test_paid_provider_with_key_is_accepted() -> None:
    s = _settings(ai_mode="cloud", llm_provider="openai", openai_api_key="sk-test-not-real")
    assert s.can_spend_money is True
    assert s.selected_paid_providers == {"openai"}


def test_disabled_audio_provider_needs_no_key() -> None:
    """A selected-but-disabled provider cannot be called, so it needs no credential."""
    s = _settings(
        ai_mode="cloud",
        llm_provider="mock",
        stt_provider="deepgram",
        text_only_mode=True,
    )
    assert s.can_spend_money is False


def test_warn_threshold_must_not_exceed_ceiling() -> None:
    with pytest.raises(ValidationError, match="WARN_ESTIMATED_PROJECT_COST_USD"):
        _settings(warn_estimated_project_cost_usd=25, max_estimated_project_cost_usd=20)


def test_override_is_ignored_outside_development() -> None:
    assert _settings(budget_guard_override=True, app_env="production").override_permitted is False
    assert _settings(budget_guard_override=True, app_env="development").override_permitted is True
