"""Value types for the voice loop.

Kept apart from the turn manager so the transport (LiveKit, Phase 13b; SIP,
Phase 15) can speak in these terms without importing the state machine.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict


class VoiceState(StrEnum):
    """Where a voice turn is. Explicit, for the same reason as every other
    state machine here: a timing bug you can name is a timing bug you can fix.
    """

    #: Waiting for the caller to say something.
    LISTENING = "LISTENING"
    #: A final transcript is with the orchestrator.
    THINKING = "THINKING"
    #: Audio is playing back to the caller.
    SPEAKING = "SPEAKING"
    #: The call is over; nothing further is processed.
    CLOSED = "CLOSED"


class CloseReason(StrEnum):
    SILENCE = "silence"
    TIMEOUT = "timeout"
    CALLER_HUNG_UP = "caller_hung_up"
    COMPLETED = "completed"
    BUDGET = "budget"


class VoiceEvent(BaseModel):
    """Something the turn manager did, for logging and for the dashboard.

    Voice failures are timing failures, and timing failures are invisible
    afterwards unless they were recorded as they happened.
    """

    model_config = ConfigDict(frozen=True)

    kind: str
    detail: str | None = None
    state: VoiceState = VoiceState.LISTENING


#: What the transport gives the turn manager to speak with. Returns a coroutine
#: -- not merely an awaitable -- because playback is run as a task so that
#: barge-in can cancel it mid-sentence. It completes when playback finishes, or
#: raises ``asyncio.CancelledError`` if it was interrupted.
SpeakFn = Callable[[str], Coroutine[Any, Any, None]]
