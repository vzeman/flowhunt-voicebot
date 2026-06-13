from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import time
from typing import Any, Callable

from fastapi import APIRouter

from .api_models import AgentTaskClaimRequest, AgentTaskReleaseRequest, AgentTaskRenewRequest
from .events import event_to_dict


@dataclass(frozen=True)
class AgentTasksApiContext:
    events: Any
    registry: Any
    tracker: Any
    validated_limit: Callable[[int], int]
    prompt_context_for_pending: Callable[[list[Any]], dict[str, Any]]
    agent_task_event_to_dict: Callable[[Any, dict[str, Any]], dict[str, Any]]


def create_agent_tasks_router(context: AgentTasksApiContext) -> APIRouter:
    router = APIRouter()

    @router.get("/agent/tasks")
    def agent_tasks(
        after: int = 0,
        call_id: str | None = None,
        limit: int = 200,
        wait_seconds: float = 0.0,
    ) -> dict[str, Any]:
        return agent_tasks_payload(context, after=after, call_id=call_id, limit=limit, wait_seconds=wait_seconds)

    @router.post("/agent/tasks/claim")
    def claim_agent_tasks(request: AgentTaskClaimRequest) -> dict[str, Any]:
        return claim_agent_tasks_payload(context, request)

    @router.post("/agent/tasks/release")
    def release_agent_tasks(request: AgentTaskReleaseRequest) -> dict[str, Any]:
        return release_agent_tasks_payload(context, request)

    @router.post("/agent/tasks/renew")
    def renew_agent_tasks(request: AgentTaskRenewRequest) -> dict[str, Any]:
        return renew_agent_tasks_payload(context, request)

    @router.get("/agent/tasks/status")
    def agent_task_status(owner: str | None = None) -> dict[str, Any]:
        return agent_task_status_payload(context, owner=owner)

    @router.get("/agent/tasks/summary")
    def agent_task_summary(
        after: int = 0,
        call_id: str | None = None,
        owner: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        return agent_task_summary_payload(context, after=after, call_id=call_id, owner=owner, limit=limit)

    return router


def agent_tasks_payload(
    context: AgentTasksApiContext,
    *,
    after: int = 0,
    call_id: str | None = None,
    limit: int = 200,
    wait_seconds: float = 0.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + min(max(float(wait_seconds), 0.0), 30.0)
    while True:
        payload = _agent_tasks_payload_now(context, after=after, call_id=call_id, limit=limit)
        if payload["pending"] or wait_seconds <= 0 or time.monotonic() >= deadline:
            return payload
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


def _agent_tasks_payload_now(
    context: AgentTasksApiContext,
    *,
    after: int = 0,
    call_id: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    checked_limit = context.validated_limit(limit)
    all_events = [
        event
        for event in context.events.list_events(after=after, limit=1000)
        if event.type == "agent_response_requested"
    ]
    active_call_ids = set(context.registry.active_call_ids())
    pending = [
        event
        for event in all_events
        if event.type == "agent_response_requested"
        and context.tracker.is_pending(event.id)
        and event.call_id in active_call_ids
        and (call_id is None or event.call_id == call_id)
    ]
    context_slice = pending[:checked_limit]
    task_context = context.events.context(call_id=call_id)
    task_context.update(context.prompt_context_for_pending(context_slice))
    return {
        "pending": [context.agent_task_event_to_dict(event, task_context) for event in context_slice],
        "context": task_context,
    }


def claim_agent_tasks_payload(context: AgentTasksApiContext, request: AgentTaskClaimRequest) -> dict[str, Any]:
    active_call_ids = set(context.registry.active_call_ids())
    eligible_event_ids = []
    for event_id in request.event_ids:
        source_event = context.events.get_event(event_id)
        if (
            source_event is not None
            and source_event.type == "agent_response_requested"
            and source_event.call_id in active_call_ids
        ):
            eligible_event_ids.append(event_id)

    claimed_event_ids = context.tracker.claim(eligible_event_ids, request.owner, request.ttl_seconds)
    for event_id in claimed_event_ids:
        source_event = context.events.get_event(event_id)
        if source_event is None:
            continue
        context.events.append(
            source_event.call_id,
            "agent_task_claimed",
            {
                "task_event_id": event_id,
                "owner": request.owner,
                "ttl_seconds": request.ttl_seconds,
            },
        )
        latency = _seconds_since_event(source_event)
        if latency is not None:
            context.events.append(
                source_event.call_id,
                "metrics",
                {
                    "name": "agent_task_pickup_latency_seconds",
                    "value": latency,
                    "task_event_id": event_id,
                    "owner": request.owner,
                },
            )
    return {
        "claimed_event_ids": claimed_event_ids,
        "owner": request.owner,
    }


def _seconds_since_event(event: Any) -> float | None:
    try:
        timestamp = datetime.fromisoformat(str(event.timestamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    now = datetime.now(timestamp.tzinfo)
    return max(0.0, (now - timestamp).total_seconds())


def release_agent_tasks_payload(context: AgentTasksApiContext, request: AgentTaskReleaseRequest) -> dict[str, Any]:
    released_event_ids = context.tracker.release_many(request.event_ids, owner=request.owner)
    for event_id in released_event_ids:
        source_event = context.events.get_event(event_id)
        if source_event is None:
            continue
        context.events.append(
            source_event.call_id,
            "agent_task_released",
            {"task_event_id": event_id, "owner": request.owner},
        )
    return {"released_event_ids": released_event_ids}


def renew_agent_tasks_payload(context: AgentTasksApiContext, request: AgentTaskRenewRequest) -> dict[str, Any]:
    renewed_event_ids = context.tracker.renew_many(request.event_ids, request.owner, request.ttl_seconds)
    for event_id in renewed_event_ids:
        source_event = context.events.get_event(event_id)
        if source_event is None:
            continue
        context.events.append(
            source_event.call_id,
            "agent_task_renewed",
            {
                "task_event_id": event_id,
                "owner": request.owner,
                "ttl_seconds": request.ttl_seconds,
            },
        )
    return {"renewed_event_ids": renewed_event_ids, "owner": request.owner}


def agent_task_status_payload(context: AgentTasksApiContext, *, owner: str | None = None) -> dict[str, Any]:
    return context.tracker.snapshot(owner=owner)


def agent_task_summary_payload(
    context: AgentTasksApiContext,
    *,
    after: int = 0,
    call_id: str | None = None,
    owner: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    checked_limit = context.validated_limit(limit)
    active_call_ids = set(context.registry.active_call_ids())
    task_events = [
        event
        for event in context.events.list_events(after=after, limit=1000, call_id=call_id)
        if event.type == "agent_response_requested"
    ]
    tasks = []
    counts: dict[str, int] = {}
    for event in task_events:
        state = context.tracker.task_state(event.id, active=event.call_id in active_call_ids)
        if owner is not None and state.get("state") == "claimed" and state.get("owner") != owner:
            continue
        entry = {
            "event": event_to_dict(event),
            **state,
        }
        tasks.append(entry)
        state_name = str(state["state"])
        counts[state_name] = counts.get(state_name, 0) + 1
    return {
        "tasks": tasks[:checked_limit],
        "counts": counts,
        "active_calls": sorted(active_call_ids),
    }
