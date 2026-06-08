from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from fastapi import APIRouter

from .api_models import CompactContextRequest
from .events import event_to_dict


@dataclass(frozen=True)
class ContextApiContext:
    events: Any
    broadcast: Callable[[Any], Awaitable[None]]


def create_context_router(context: ContextApiContext) -> APIRouter:
    router = APIRouter()

    @router.get("/context")
    def get_context(call_id: str | None = None) -> dict[str, Any]:
        return context_payload(context, call_id=call_id)

    @router.post("/context/compact")
    async def compact_context(request: CompactContextRequest) -> dict[str, Any]:
        return await compact_context_payload(context, request)

    return router


def context_payload(context: ContextApiContext, *, call_id: str | None = None) -> dict[str, Any]:
    return context.events.context(call_id=call_id)


async def compact_context_payload(context: ContextApiContext, request: CompactContextRequest) -> dict[str, Any]:
    event = context.events.replace_summary(request.summary, call_id=request.call_id)
    await context.broadcast(event)
    return {"event": event_to_dict(event)}
