from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class AgentResponseRequest(BaseModel):
    text: str
    response_to_event_id: int | None = None


class CompactContextRequest(BaseModel):
    summary: str
    call_id: str = "system"


class CallControlRequest(BaseModel):
    action: str
    target: str | None = None
    digit: str | None = None
    response_to_event_id: int | None = None


class PlaybackInterruptRequest(BaseModel):
    reason: str = "agent_requested"
    response_to_event_id: int | None = None


class AgentToolRequest(BaseModel):
    arguments: dict[str, Any] = {}


class AgentTaskClaimRequest(BaseModel):
    event_ids: list[int]
    owner: str = "agent"
    ttl_seconds: float = 60.0


class AgentTaskReleaseRequest(BaseModel):
    event_ids: list[int]
    owner: str | None = None
