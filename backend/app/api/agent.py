"""Developer chat endpoint.

The single entry point to the agent runtime. The CLI and, later, the voice
agent call the same ``handle_turn``, which is what keeps text and voice from
drifting apart (ADR 005).

Local development only: there is no authentication here, and the dashboard is
not exposed. Staff-facing access control is noted as a gap in SAFETY.md.
"""

from __future__ import annotations

from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.agents.factory import Runtime, build_runtime
from app.agents.state import SessionChannel, SessionSnapshot
from app.agents.trace import TurnTrace
from app.services.base import NotFoundError

router = APIRouter(prefix="/api/agent", tags=["agent"])


@lru_cache(maxsize=1)
def get_runtime() -> Runtime:
    """Process-wide runtime, so sessions survive between requests."""
    return build_runtime()


class StartSessionRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    channel: SessionChannel = SessionChannel.TEXT


class TurnRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    utterance: str = Field(min_length=1, max_length=2000)


class TurnResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    message: str
    session: SessionSnapshot
    trace: TurnTrace


class SessionDetail(BaseModel):
    model_config = ConfigDict(frozen=True)

    session: SessionSnapshot
    traces: list[TurnTrace]


@router.post("/sessions", response_model=SessionSnapshot, status_code=201)
async def start_session(
    request: StartSessionRequest, runtime: Runtime = Depends(get_runtime)
) -> SessionSnapshot:
    return runtime.sessions.create(channel=request.channel).snapshot()


@router.post("/sessions/{session_id}/turns", response_model=TurnResponse)
async def submit_turn(
    session_id: str, request: TurnRequest, runtime: Runtime = Depends(get_runtime)
) -> TurnResponse:
    """Submit one utterance and receive the response plus its full trace."""
    try:
        session = runtime.sessions.get(session_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if not session.is_active:
        raise HTTPException(status_code=409, detail="session has ended")

    result = await runtime.orchestrator.handle_turn(session, request.utterance)
    return TurnResponse(message=result.message, session=session.snapshot(), trace=result.trace)


@router.get("/sessions/{session_id}", response_model=SessionDetail)
async def get_session(session_id: str, runtime: Runtime = Depends(get_runtime)) -> SessionDetail:
    try:
        session = runtime.sessions.get(session_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return SessionDetail(session=session.snapshot(), traces=runtime.traces.for_session(session_id))


@router.post("/sessions/{session_id}/end", response_model=SessionSnapshot)
async def end_session(session_id: str, runtime: Runtime = Depends(get_runtime)) -> SessionSnapshot:
    try:
        return runtime.sessions.end(session_id).snapshot()
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
