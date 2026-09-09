"""Chat completions in the OpenAI wire format.

Talks to the REST API over ``httpx`` rather than the vendor SDK. The surface
used here is one endpoint and a stable JSON shape; an SDK would add a
dependency, its own retry and telemetry behaviour, and a second place for
credentials to leak, in exchange for very little.

That format is not OpenAI's alone -- Groq serves it too -- so this adapter is
parameterised by base URL and provider name rather than hard-wired to one
vendor. Adding an OpenAI-compatible provider is then a settings entry, not a
new adapter. Where a vendor differs, it says so explicitly rather than being
discovered in production: ``supports_message_name`` is the first such case.

Usage is metered from the response's own ``usage`` block, not estimated: the
budget guard is only as good as the numbers it reads (ADR 006).
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.ai.providers.base import (
    ChatMessage,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    ProposedToolCall,
    ProviderUnavailableError,
    ToolCallRequest,
    ToolCallResponse,
)
from app.ai.usage import INPUT_TOKENS, OUTPUT_TOKENS, REQUESTS, UsageLedger
from app.observability.logging import get_logger

logger = get_logger(__name__)

DEFAULT_BASE_URL = "https://api.openai.com/v1"

#: Retried once, because they are transient by definition. Everything else
#: fails immediately: retrying a 400 just spends money twice.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class OpenAILLMProvider:
    """``LLMProvider`` for any OpenAI-compatible chat completions endpoint."""

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4.1-mini",
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        ledger: UsageLedger | None = None,
        client: httpx.AsyncClient | None = None,
        name: str = "openai",
        supports_message_name: bool = True,
        reasoning_effort: str | None = None,
    ) -> None:
        if not api_key:
            # Reachable only by constructing the adapter directly; settings
            # validation catches the configured case at startup.
            raise ValueError("OpenAILLMProvider requires an API key")
        self.name = name
        self.model = model
        self.ledger = ledger
        #: Groq rejects ``messages[].name``. Only tool-repair messages carry
        #: one, so an unfiltered payload works until the first time a model
        #: gets its arguments wrong -- the worst moment to find out.
        self.supports_message_name = supports_message_name
        #: Reasoning models spend output tokens thinking before they answer, so
        #: a low cap can return an empty message that cost a full budget. Set
        #: this for such a model; leave it None for one that has no such
        #: parameter, which would reject it.
        self.reasoning_effort = reasoning_effort
        # Credentials go on the request, not on the client. A client passed in
        # by a caller (a test, a shared pool) would otherwise carry no key, and
        # the adapter would look authenticated while sending nothing.
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------- requests
    async def generate(self, request: LLMRequest) -> LLMResponse:
        payload = self._payload(request)
        body = await self._post(payload, session_id=request.session_id)
        choice = self._first_choice(body)
        usage = self._usage(body)
        self._meter(usage, request.session_id)
        return LLMResponse(
            text=(choice.get("message") or {}).get("content") or "",
            usage=usage,
            finish_reason=choice.get("finish_reason"),
        )

    async def tool_call(self, request: ToolCallRequest) -> ToolCallResponse:
        payload = self._payload(request)
        payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in request.tools
        ]
        payload["tool_choice"] = request.tool_choice

        body = await self._post(payload, session_id=request.session_id)
        choice = self._first_choice(body)
        message = choice.get("message") or {}
        usage = self._usage(body)
        self._meter(usage, request.session_id)

        return ToolCallResponse(
            text=message.get("content"),
            tool_calls=[
                call
                for call in (self._proposed(raw) for raw in message.get("tool_calls") or [])
                if call is not None
            ],
            usage=usage,
            finish_reason=choice.get("finish_reason"),
        )

    # -------------------------------------------------------------- internals
    def _payload(self, request: LLMRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [self._message(message) for message in request.messages],
            "max_completion_tokens": request.max_output_tokens,
            "temperature": request.temperature,
        }
        if self.reasoning_effort is not None:
            payload["reasoning_effort"] = self.reasoning_effort
        return payload

    def _message(self, message: ChatMessage) -> dict[str, Any]:
        """One message, with fields this endpoint does not accept removed."""
        fields = {key: value for key, value in message.model_dump().items() if value is not None}
        if not self.supports_message_name:
            fields.pop("name", None)
        return fields

    async def _post(self, payload: dict[str, Any], session_id: str | None) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in (1, 2):
            try:
                response = await self._client.post(
                    "/chat/completions", json=payload, headers=self._headers
                )
            except httpx.HTTPError as exc:
                last_error = exc
                logger.warning(
                    "llm_request_failed", provider=self.name, attempt=attempt, error=str(exc)
                )
                if attempt == 1:
                    continue
                raise ProviderUnavailableError(f"{self.name} request failed: {exc}") from exc

            if response.status_code in RETRYABLE_STATUS and attempt == 1:
                logger.warning(
                    "openai_retryable_status", status=response.status_code, attempt=attempt
                )
                continue
            if response.status_code >= 400:
                raise ProviderUnavailableError(self._error_message(response))

            try:
                body: dict[str, Any] = response.json()
            except ValueError as exc:
                raise ProviderUnavailableError(f"{self.name} returned a non-JSON body") from exc
            logger.info(
                "llm_response",
                provider=self.name,
                session_id=session_id,
                model=self.model,
                status=response.status_code,
            )
            return body

        raise ProviderUnavailableError(f"{self.name} request failed: {last_error}")

    def _error_message(self, response: httpx.Response) -> str:
        """A useful message that cannot contain the request or the key."""
        detail = ""
        try:
            payload = response.json()
            detail = str((payload.get("error") or {}).get("message", ""))[:200]
        except ValueError:
            detail = ""
        if response.status_code == 401:
            return f"{self.name} rejected the API key (401). Check {self.name.upper()}_API_KEY."
        if response.status_code == 429:
            return f"{self.name} rate limit reached (429){f': {detail}' if detail else ''}"
        return f"{self.name} returned {response.status_code}{f': {detail}' if detail else ''}"

    @staticmethod
    def _first_choice(body: dict[str, Any]) -> dict[str, Any]:
        choices = body.get("choices") or []
        if not choices:
            raise ProviderUnavailableError("the model returned no choices")
        first: dict[str, Any] = choices[0]
        return first

    @staticmethod
    def _usage(body: dict[str, Any]) -> LLMUsage:
        usage = body.get("usage") or {}
        return LLMUsage(
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
        )

    @staticmethod
    def _proposed(raw: dict[str, Any]) -> ProposedToolCall | None:
        """Shape one tool call, or drop it if the model returned nonsense.

        Arguments are parsed but **not** validated here: what comes back is a
        proposal, and deciding whether it is executable belongs to the tool
        layer (docs/agent-tools.md). Unparseable JSON is dropped rather than
        guessed at -- a repaired-by-the-adapter call is a call nobody reviewed.
        """
        function = raw.get("function") or {}
        name = function.get("name")
        call_id = raw.get("id")
        if not name or not call_id:
            return None
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except json.JSONDecodeError:
            logger.warning("llm_tool_arguments_unparseable", tool=name)
            return None
        if not isinstance(arguments, dict):
            return None
        return ProposedToolCall(tool_call_id=str(call_id), name=str(name), arguments=arguments)

    def _meter(self, usage: LLMUsage, session_id: str | None) -> None:
        if self.ledger is None:
            return
        self.ledger.record(self.name, INPUT_TOKENS, usage.input_tokens, session_id)
        self.ledger.record(self.name, OUTPUT_TOKENS, usage.output_tokens, session_id)
        self.ledger.record(self.name, REQUESTS, 1, session_id)
