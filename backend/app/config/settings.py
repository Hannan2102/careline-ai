"""Application configuration.

One ``Settings`` object, loaded once and injected. No other module reads the
environment directly -- that keeps configuration testable and makes invalid
combinations detectable at startup rather than at the first request.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AIMode(StrEnum):
    CLOUD = "cloud"
    LOCAL = "local"
    MOCK = "mock"


class EHRProviderName(StrEnum):
    MEMORY = "memory"
    LOCAL = "local"
    EPIC = "epic"


LLMProviderName = Literal["mock", "openai", "groq", "ollama"]
STTProviderName = Literal["mock", "deepgram", "whisper"]
TTSProviderName = Literal["mock", "elevenlabs", "piper"]

#: Providers that cost money. Used by the budget guard and by startup validation.
#:
#: Groq is deliberately absent: its developer tier is free, rate-limited rather
#: than metered. Adding a card to a Groq account moves it to a paid tier, and
#: at that point it belongs in this set with rates in ``ai/usage.py`` -- the
#: guard cannot enforce a ceiling on spending it does not know about.
PAID_PROVIDERS: frozenset[str] = frozenset({"openai", "deepgram", "elevenlabs"})


class Settings(BaseSettings):
    """Runtime configuration.

    Defaults are deliberately the *safe* ones: text-only, mock providers, no
    possibility of a paid API call. A developer must opt in to spending money.
    """

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- Application -----------------------------------------------------
    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    #: Browser origins allowed to call this API. The admin dashboard runs on a
    #: different port in development, so it needs to be named explicitly.
    dashboard_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]

    # --- Persistence -----------------------------------------------------
    database_url: str = "sqlite+aiosqlite:///./careline.db"
    #: Off in tests unless a database is explicitly provided; on by default so
    #: the audit trail and usage ledger survive a restart in normal use.
    persistence_enabled: bool = True

    # --- EHR -------------------------------------------------------------
    ehr_provider: EHRProviderName = EHRProviderName.MEMORY
    fhir_base_url: str = "http://localhost:8080/fhir"
    fhir_timeout_seconds: float = 10.0

    # --- AI providers ----------------------------------------------------
    ai_mode: AIMode = AIMode.MOCK
    llm_provider: LLMProviderName = "mock"
    stt_provider: STTProviderName = "mock"
    tts_provider: TTSProviderName = "mock"

    openai_api_key: str | None = None
    openai_model: str = "gpt-4.1-mini"

    # Groq serves an OpenAI-compatible API, so it reuses that adapter with a
    # different base URL rather than needing one of its own.
    groq_api_key: str | None = None
    groq_model: str = "openai/gpt-oss-20b"
    groq_base_url: str = "https://api.groq.com/openai/v1"
    #: gpt-oss models reason before answering, spending output tokens on it. At
    #: "low" this extraction task answers in 130-500 ms; unset, a small token
    #: cap can return an empty message that cost the whole budget.
    groq_reasoning_effort: str | None = "low"
    deepgram_api_key: str | None = None
    deepgram_model: str = "nova-3"
    elevenlabs_api_key: str | None = None
    elevenlabs_voice_id: str | None = None
    elevenlabs_model: str = "eleven_flash_v2_5"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3:8b"

    # --- Voice transport / telephony -------------------------------------
    livekit_url: str | None = None
    livekit_api_key: str | None = None
    livekit_api_secret: str | None = None
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_phone_number: str | None = None

    # --- Mode switches ---------------------------------------------------
    text_only_mode: bool = True
    voice_enabled: bool = False
    stt_enabled: bool = False
    tts_enabled: bool = False

    # --- Budget controls (see COSTS.md) ----------------------------------
    max_estimated_project_cost_usd: Decimal = Decimal("20")
    warn_estimated_project_cost_usd: Decimal = Decimal("15")
    max_estimated_session_cost_usd: Decimal = Decimal("1")
    max_llm_output_tokens: int = Field(default=400, gt=0)
    max_conversation_turns: int = Field(default=20, gt=0)
    max_tts_characters_per_session: int = Field(default=3000, gt=0)
    max_stt_minutes_per_session: int = Field(default=10, gt=0)
    budget_guard_override: bool = False

    # ---------------------------------------------------------------- rules
    @model_validator(mode="after")
    def _apply_mode_precedence(self) -> Settings:
        """Resolve mode switches deterministically.

        ``AI_MODE=mock`` forces every provider to its mock, and
        ``TEXT_ONLY_MODE=true`` hard-disables audio. Both are one-way switches
        toward *cheaper*, so a misconfiguration can never accidentally spend.
        """
        if self.ai_mode is AIMode.MOCK:
            object.__setattr__(self, "llm_provider", "mock")
            object.__setattr__(self, "stt_provider", "mock")
            object.__setattr__(self, "tts_provider", "mock")

        if self.text_only_mode:
            object.__setattr__(self, "voice_enabled", False)
            object.__setattr__(self, "stt_enabled", False)
            object.__setattr__(self, "tts_enabled", False)
        return self

    @model_validator(mode="after")
    def _require_credentials_for_paid_providers(self) -> Settings:
        """Fail fast when a paid provider is selected without a key.

        Discovering this at the first live call means discovering it mid-demo.
        """
        missing: list[str] = []
        if self.llm_provider == "openai" and not self.openai_api_key:
            missing.append("OPENAI_API_KEY (LLM_PROVIDER=openai)")
        if self.llm_provider == "groq" and not self.groq_api_key:
            missing.append("GROQ_API_KEY (LLM_PROVIDER=groq)")
        if self.stt_provider == "deepgram" and self.stt_enabled and not self.deepgram_api_key:
            missing.append("DEEPGRAM_API_KEY (STT_PROVIDER=deepgram)")
        if self.tts_provider == "elevenlabs" and self.tts_enabled and not self.elevenlabs_api_key:
            missing.append("ELEVENLABS_API_KEY (TTS_PROVIDER=elevenlabs)")
        if missing:
            raise ValueError(
                "Paid provider selected without credentials: "
                + ", ".join(missing)
                + ". Set the key, or use AI_MODE=mock for zero-cost development."
            )
        return self

    @model_validator(mode="after")
    def _budget_thresholds_are_ordered(self) -> Settings:
        if self.warn_estimated_project_cost_usd > self.max_estimated_project_cost_usd:
            raise ValueError(
                "WARN_ESTIMATED_PROJECT_COST_USD must not exceed MAX_ESTIMATED_PROJECT_COST_USD"
            )
        return self

    # ------------------------------------------------------------- helpers
    @property
    def selected_paid_providers(self) -> set[str]:
        """Paid providers that are both selected and enabled."""
        selected: set[str] = set()
        if self.llm_provider in PAID_PROVIDERS:
            selected.add(self.llm_provider)
        if self.stt_enabled and self.stt_provider in PAID_PROVIDERS:
            selected.add(self.stt_provider)
        if self.tts_enabled and self.tts_provider in PAID_PROVIDERS:
            selected.add(self.tts_provider)
        return selected

    @property
    def can_spend_money(self) -> bool:
        """True when the current configuration makes a paid call possible at all."""
        return bool(self.selected_paid_providers)

    @property
    def override_permitted(self) -> bool:
        """The budget-guard escape hatch is only honoured in development."""
        return self.budget_guard_override and self.app_env == "development"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor; the FastAPI dependency and scripts share it."""
    return Settings()
