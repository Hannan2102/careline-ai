"""Constructing AI providers.

The only module that imports a concrete provider. Everything else depends on
the Protocols in ``base.py`` (docs/provider-abstraction.md, rules 1-2).

Two gates sit between configuration and a paid call:

* **selection** -- ``AI_MODE=mock`` forces mocks, and ``TEXT_ONLY_MODE`` stops
  audio providers being constructed at all;
* **budget** -- the guard is consulted here, and again before every individual
  call by the wrappers below. Checking only at construction would let a long
  session run past the per-session ceiling, since a provider is built once and
  used for the whole conversation.

Blocking is never failing: a blocked provider is replaced by its mock and the
system keeps working in text mode (ADR 006).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

from app.ai.budget_guard import BudgetGuard
from app.ai.providers.base import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ProviderUnavailableError,
    STTProvider,
    ToolCallRequest,
    ToolCallResponse,
    Transcript,
    TTSProvider,
    VoiceSpec,
)
from app.ai.providers.llm.mock import MockLLMProvider, MockSTTProvider, MockTTSProvider
from app.ai.usage import UsageLedger, get_usage_ledger
from app.config.settings import Settings, get_settings
from app.observability.logging import get_logger

logger = get_logger(__name__)


# --------------------------------------------------------------------------
# Per-call budget enforcement
# --------------------------------------------------------------------------


class GuardedLLMProvider:
    """A paid LLM provider that asks the guard before every call.

    Falls back rather than raising: mid-conversation is the worst possible
    moment to discover a ceiling, and a caller who gets a deterministic answer
    is better served than one who gets an error.
    """

    def __init__(self, paid: LLMProvider, fallback: LLMProvider, guard: BudgetGuard) -> None:
        self.paid = paid
        self.fallback = fallback
        self.guard = guard
        self.name = paid.name

    def _choose(self, session_id: str | None) -> LLMProvider:
        decision = self.guard.check(self.paid.name, session_id)
        if decision.allowed:
            if decision.status == "warn" and decision.reason:
                logger.warning("budget_warning", provider=self.paid.name, reason=decision.reason)
            return self.paid
        logger.warning(
            "budget_blocked_falling_back",
            provider=self.paid.name,
            fallback=self.fallback.name,
            reason=decision.reason,
        )
        return self.fallback

    async def generate(self, request: LLMRequest) -> LLMResponse:
        provider = self._choose(request.session_id)
        try:
            return await provider.generate(request)
        except ProviderUnavailableError as exc:
            return await self._degrade(exc, provider).generate(request)

    async def tool_call(self, request: ToolCallRequest) -> ToolCallResponse:
        provider = self._choose(request.session_id)
        try:
            return await provider.tool_call(request)
        except ProviderUnavailableError as exc:
            return await self._degrade(exc, provider).tool_call(request)

    def _degrade(self, exc: ProviderUnavailableError, attempted: LLMProvider) -> LLMProvider:
        """Fall back after a provider failure, loudly.

        A rate limit or an outage should not end a call. This is deliberately
        not silent: the log line is the only thing that distinguishes "the
        deterministic path answered because we chose it" from "because the
        vendor was down", and a free tier makes the second much more likely.
        """
        if attempted is self.fallback:
            raise exc
        logger.error(
            "llm_unavailable_falling_back",
            provider=attempted.name,
            fallback=self.fallback.name,
            error=str(exc),
        )
        return self.fallback


class GuardedTTSProvider:
    """A paid TTS provider that asks the guard before every synthesis."""

    def __init__(
        self, paid: TTSProvider, fallback: TTSProvider, guard: BudgetGuard, session_id: str | None
    ) -> None:
        self.paid = paid
        self.fallback = fallback
        self.guard = guard
        self.session_id = session_id
        self.name = paid.name
        #: Told when synthesis degrades, so a transport can say so.
        #:
        #: The fallback emits silence-shaped bytes, which is the correct
        #: behaviour for an offline test and the worst possible behaviour on a
        #: live call: the agent answers perfectly and the caller hears nothing,
        #: which is indistinguishable from a crashed server, a dead microphone
        #: or a broken socket. It cost three debugging sessions to recognise,
        #: and a real caller would simply hang up. Logging it server-side is
        #: not enough -- nobody on the call can read the logs.
        self.on_degraded: Callable[[str], Awaitable[None]] | None = None

    async def _degraded(self, reason: str) -> None:
        if self.on_degraded is None:
            return
        try:
            await self.on_degraded(reason)
        except Exception as exc:
            # Never let the notification break the synthesis it is describing.
            logger.warning("tts_degraded_notice_failed", error=str(exc))

    async def synthesize_stream(self, text: str, voice: VoiceSpec) -> AsyncIterator[bytes]:
        decision = self.guard.check(self.paid.name, self.session_id)
        provider = self.paid if decision.allowed else self.fallback
        if not decision.allowed:
            logger.warning(
                "budget_blocked_falling_back", provider=self.paid.name, reason=decision.reason
            )
            await self._degraded(f"Speech budget reached: {decision.reason}")
        try:
            async for chunk in provider.synthesize_stream(text, voice):
                yield chunk
        except ProviderUnavailableError as exc:
            # A free tier makes an outage or a rate limit likely, and a caller
            # mid-sentence is the worst moment to raise. Only safe to retry on
            # the fallback because nothing has been yielded yet when the
            # failure is the request itself -- which is when it happens: the
            # adapters check status before the first chunk.
            if provider is self.fallback:
                raise
            logger.error(
                "tts_unavailable_falling_back",
                provider=provider.name,
                fallback=self.fallback.name,
                error=str(exc),
            )
            await self._degraded(f"{provider.name} speech unavailable: {exc}")
            async for chunk in self.fallback.synthesize_stream(text, voice):
                yield chunk


class GuardedSTTProvider:
    """A paid STT provider that asks the guard before opening a stream."""

    def __init__(
        self, paid: STTProvider, fallback: STTProvider, guard: BudgetGuard, session_id: str | None
    ) -> None:
        self.paid = paid
        self.fallback = fallback
        self.guard = guard
        self.session_id = session_id
        self.name = paid.name

    async def transcribe_stream(self, audio: AsyncIterator[bytes]) -> AsyncIterator[Transcript]:
        decision = self.guard.check(self.paid.name, self.session_id)
        provider = self.paid if decision.allowed else self.fallback
        if not decision.allowed:
            logger.warning(
                "budget_blocked_falling_back", provider=self.paid.name, reason=decision.reason
            )
        async for transcript in provider.transcribe_stream(audio):
            yield transcript


# --------------------------------------------------------------------------
# Construction
# --------------------------------------------------------------------------


def build_llm_provider(
    settings: Settings | None = None,
    ledger: UsageLedger | None = None,
    session_id: str | None = None,
) -> LLMProvider:
    """The configured LLM provider, wrapped in the budget guard when paid."""
    resolved = settings or get_settings()
    usage = ledger or get_usage_ledger()
    mock = MockLLMProvider(ledger=usage)

    if resolved.llm_provider == "openai":
        if not resolved.openai_api_key:
            # Startup validation makes this unreachable in a configured app;
            # falling back beats raising if it ever is reached.
            logger.error("openai_selected_without_key_falling_back_to_mock")
            return mock
        from app.ai.providers.llm.openai import OpenAILLMProvider

        paid = OpenAILLMProvider(
            api_key=resolved.openai_api_key,
            model=resolved.openai_model,
            ledger=usage,
        )
        logger.info("llm_provider_built", provider=paid.name, model=resolved.openai_model)
        return GuardedLLMProvider(paid, mock, BudgetGuard(resolved, usage))

    if resolved.llm_provider == "groq":
        if not resolved.groq_api_key:
            logger.error("groq_selected_without_key_falling_back_to_mock")
            return mock
        from app.ai.providers.llm.openai import OpenAILLMProvider

        # Free tier: rate-limited rather than metered, so it is not in
        # PAID_PROVIDERS and the guard will not block it. Tokens are still
        # recorded, priced at zero, so the metering path stays exercised.
        groq = OpenAILLMProvider(
            api_key=resolved.groq_api_key,
            model=resolved.groq_model,
            base_url=resolved.groq_base_url,
            ledger=usage,
            name="groq",
            supports_message_name=False,
            reasoning_effort=resolved.groq_reasoning_effort,
        )
        logger.info("llm_provider_built", provider=groq.name, model=resolved.groq_model)
        # Wrapped anyway: the wrapper is what degrades to the deterministic
        # path when the free tier's rate limit is reached.
        return GuardedLLMProvider(groq, mock, BudgetGuard(resolved, usage))

    if resolved.llm_provider == "ollama":
        # Phase 17. Selecting it today gets the mock rather than a stub that
        # pretends to be a local model.
        logger.warning("ollama_not_implemented_using_mock")
        return mock

    return mock


def build_stt_provider(
    settings: Settings | None = None,
    ledger: UsageLedger | None = None,
    session_id: str | None = None,
) -> STTProvider | None:
    """The configured STT provider, or ``None`` when speech input is off."""
    resolved = settings or get_settings()
    if not resolved.stt_enabled:
        return None

    usage = ledger or get_usage_ledger()
    mock = MockSTTProvider()

    if resolved.stt_provider == "deepgram" and resolved.deepgram_api_key:
        from app.ai.providers.stt.deepgram import DeepgramSTTProvider

        paid = DeepgramSTTProvider(
            api_key=resolved.deepgram_api_key,
            model=resolved.deepgram_model,
            ledger=usage,
            session_id=session_id,
        )
        logger.info("stt_provider_built", provider=paid.name, model=resolved.deepgram_model)
        return GuardedSTTProvider(paid, mock, BudgetGuard(resolved, usage), session_id)

    return mock


def build_tts_provider(
    settings: Settings | None = None,
    ledger: UsageLedger | None = None,
    session_id: str | None = None,
) -> TTSProvider | None:
    """The configured TTS provider, or ``None`` when speech output is off."""
    resolved = settings or get_settings()
    if not resolved.tts_enabled:
        return None

    usage = ledger or get_usage_ledger()
    mock = MockTTSProvider()

    if resolved.tts_provider == "groq":
        if not resolved.groq_api_key:
            logger.error("groq_tts_selected_without_key_falling_back_to_mock")
            return mock
        from app.ai.providers.tts.groq import GroqTTSProvider

        # Free tier, like the Groq LLM: not in PAID_PROVIDERS, so the guard
        # will not block it. Wrapped anyway -- the wrapper is what degrades to
        # the deterministic path when the free tier rate-limits us.
        groq_tts = GroqTTSProvider(
            api_key=resolved.groq_api_key,
            voice=resolved.groq_tts_voice,
            model=resolved.groq_tts_model,
            base_url=resolved.groq_base_url,
            ledger=usage,
            session_id=session_id,
        )
        logger.info("tts_provider_built", provider=groq_tts.name, model=resolved.groq_tts_model)
        return GuardedTTSProvider(groq_tts, mock, BudgetGuard(resolved, usage), session_id)

    if resolved.tts_provider == "deepgram":
        if not resolved.deepgram_api_key:
            logger.error("deepgram_tts_selected_without_key_falling_back_to_mock")
            return mock
        from app.ai.providers.tts.deepgram import DeepgramTTSProvider

        # The same key already buys recognition. Metered rather than
        # rate-limited, so unlike Groq this one is genuinely guarded by the
        # budget: see RATES in ai/usage.py.
        deepgram_tts = DeepgramTTSProvider(
            api_key=resolved.deepgram_api_key,
            model=resolved.deepgram_tts_model,
            ledger=usage,
            session_id=session_id,
        )
        logger.info(
            "tts_provider_built", provider=deepgram_tts.name, model=resolved.deepgram_tts_model
        )
        return GuardedTTSProvider(deepgram_tts, mock, BudgetGuard(resolved, usage), session_id)

    if (
        resolved.tts_provider == "elevenlabs"
        and resolved.elevenlabs_api_key
        and resolved.elevenlabs_voice_id
    ):
        from app.ai.providers.tts.elevenlabs import ElevenLabsTTSProvider

        paid = ElevenLabsTTSProvider(
            api_key=resolved.elevenlabs_api_key,
            voice_id=resolved.elevenlabs_voice_id,
            model=resolved.elevenlabs_model,
            ledger=usage,
            session_id=session_id,
        )
        logger.info("tts_provider_built", provider=paid.name, model=resolved.elevenlabs_model)
        return GuardedTTSProvider(paid, mock, BudgetGuard(resolved, usage), session_id)

    return mock
