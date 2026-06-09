from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .event_catalog import event_catalog, event_catalog_integrity_issues
from .events import event_to_dict
from .internal_auth import validate_internal_api_key
from .metrics import summarize_metrics


@dataclass(frozen=True)
class EventsApiContext:
    events: Any
    transcripts: Any
    durable_call_events: Callable[..., list[Any]]
    validated_limit: Callable[[int], int]
    hub: Any = None
    settings: Any = None
    internal_keys: Any = None


def create_events_router(context: EventsApiContext) -> APIRouter:
    router = APIRouter()

    @router.get("/events")
    def list_events(after: int = 0, call_id: str | None = None, limit: int = 200) -> dict[str, Any]:
        return events_payload(context, after=after, call_id=call_id, limit=limit)

    @router.get("/events/catalog")
    def list_event_catalog() -> dict[str, Any]:
        return {"events": event_catalog(), "integrity_issues": event_catalog_integrity_issues()}

    @router.get("/metrics")
    def metrics(call_id: str | None = None) -> dict[str, Any]:
        return metrics_payload(context, call_id=call_id)

    @router.websocket("/ws/events")
    async def websocket_events(websocket: WebSocket) -> None:
        if context.settings is not None and context.settings.internal_auth_enabled:
            result = validate_internal_api_key(
                websocket.headers.get(context.settings.internal_auth_header),
                context.internal_keys or (),
                "diagnostics:read",
            )
            if not result.ok:
                context.events.append(
                    "system",
                    "internal_api_auth_denied",
                    {
                        "method": "WEBSOCKET",
                        "path": "/ws/events",
                        "reason": result.reason,
                        "scope": result.scope,
                        **({"key_id": result.key.key_id} if result.key is not None else {}),
                    },
                )
                await websocket.close(code=1008, reason=result.reason)
                return
        await context.hub.connect(websocket)
        last_id = 0
        try:
            while True:
                new_events = context.events.list_events(after=last_id, limit=100)
                for event in new_events:
                    await websocket.send_json(event_to_dict(event))
                    last_id = max(last_id, event.id)
                await asyncio.sleep(0.25)
        except WebSocketDisconnect:
            context.hub.disconnect(websocket)

    return router


def events_payload(
    context: EventsApiContext,
    *,
    after: int = 0,
    call_id: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    checked_limit = context.validated_limit(limit)
    if call_id:
        source_events = context.durable_call_events(
            context.events,
            context.transcripts,
            call_id,
            after=after,
            limit=checked_limit,
        )
    else:
        source_events = context.events.list_events(after=after, limit=checked_limit)
    result = [event_to_dict(event) for event in source_events]
    return {"events": result}


def metrics_payload(context: EventsApiContext, *, call_id: str | None = None) -> dict[str, Any]:
    return summarize_metrics(context.events.list_events(call_id=call_id, limit=1000))
