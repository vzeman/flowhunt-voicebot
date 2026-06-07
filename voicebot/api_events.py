from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter

from .event_catalog import event_catalog, event_catalog_integrity_issues
from .events import event_to_dict
from .metrics import summarize_metrics


@dataclass(frozen=True)
class EventsApiContext:
    events: Any
    transcripts: Any
    durable_call_events: Callable[..., list[Any]]
    validated_limit: Callable[[int], int]


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
