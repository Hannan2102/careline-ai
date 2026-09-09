"""Structured logging.

JSON logs correlated by ``session_id``, ``call_id``, ``turn_id``,
``tool_call_id``, and ``patient_ref``. Patient *references* are logged;
demographics, transcript content flagged as sensitive, and provider
credentials are not.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

#: Never log these, wherever they appear in an event dict.
REDACTED_KEYS: frozenset[str] = frozenset(
    {
        "openai_api_key",
        "deepgram_api_key",
        "elevenlabs_api_key",
        "livekit_api_secret",
        "twilio_auth_token",
        "authorization",
        "api_key",
        "password",
    }
)


def _redact(_logger: Any, _name: str, event: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    for key in list(event):
        if key.lower() in REDACTED_KEYS:
            event[key] = "***redacted***"
    return event


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    """Configure structlog once, at startup."""
    logging.basicConfig(
        format="%(message)s", stream=sys.stdout, level=getattr(logging, level.upper(), logging.INFO)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer()
            if json_output
            else structlog.dev.ConsoleRenderer(colors=True),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]


def bind_trace(**values: Any) -> None:
    """Bind correlation ids for the current async context."""
    structlog.contextvars.bind_contextvars(**values)


def clear_trace() -> None:
    structlog.contextvars.clear_contextvars()
