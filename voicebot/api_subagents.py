from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, HTTPException

from .api_models import (
    SpeculativeSubagentCancelRequest,
    SpeculativeSubagentConfirmRequest,
    SpeculativeSubagentTaskRequest,
    SubagentTaskCancelRequest,
    SubagentTaskSubmitRequest,
)
from .subagents import SubagentTaskRequest, subagent_task_to_dict


@dataclass(frozen=True)
class SubagentsApiContext:
    subagent_coordinator: Any
    subagent_lifecycle: Any
    require_workspace_access: Callable[[str], None]
    notify_subagent_terminal_task: Callable[[Any], Awaitable[None]]


def create_subagents_router(context: SubagentsApiContext) -> APIRouter:
    router = APIRouter()

    @router.get("/subagent/tasks")
    def subagent_tasks(workspace_id: str | None = None, session_id: str | None = None) -> dict[str, Any]:
        return subagent_tasks_payload(context, workspace_id=workspace_id, session_id=session_id)

    @router.get("/subagent/providers")
    def subagent_providers() -> dict[str, Any]:
        return subagent_providers_payload(context)

    @router.post("/subagent/tasks")
    def submit_subagent_task(request: SubagentTaskSubmitRequest) -> dict[str, Any]:
        return submit_subagent_task_payload(context, request)

    @router.post("/subagent/tasks/speculative")
    def submit_speculative_subagent_task(request: SpeculativeSubagentTaskRequest) -> dict[str, Any]:
        return submit_speculative_subagent_task_payload(context, request)

    @router.post("/subagent/tasks/{task_id}/confirm-speculative")
    async def confirm_speculative_subagent_task(
        task_id: str,
        request: SpeculativeSubagentConfirmRequest,
    ) -> dict[str, Any]:
        return await confirm_speculative_subagent_task_payload(context, task_id, request)

    @router.post("/subagent/tasks/{task_id}/cancel-speculative")
    def cancel_speculative_subagent_task(
        task_id: str,
        request: SpeculativeSubagentCancelRequest,
    ) -> dict[str, Any]:
        return cancel_speculative_subagent_task_payload(context, task_id, request)

    @router.post("/subagent/tasks/{task_id}/cancel")
    def cancel_subagent_task(task_id: str, request: SubagentTaskCancelRequest) -> dict[str, Any]:
        return cancel_subagent_task_payload(context, task_id, request)

    @router.get("/subagent/tasks/lifecycle")
    def subagent_task_lifecycle(workspace_id: str | None = None, session_id: str | None = None) -> dict[str, Any]:
        return subagent_task_lifecycle_payload(context, workspace_id=workspace_id, session_id=session_id)

    return router


def subagent_tasks_payload(
    context: SubagentsApiContext,
    *,
    workspace_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    coordinator = require_subagent_coordinator(context)
    tasks = coordinator.store.list(workspace_id=workspace_id, session_id=session_id)
    return {"tasks": [subagent_task_to_dict(task) for task in tasks]}


def subagent_providers_payload(context: SubagentsApiContext) -> dict[str, Any]:
    return require_subagent_coordinator(context).provider_catalog()


def submit_subagent_task_payload(context: SubagentsApiContext, request: SubagentTaskSubmitRequest) -> dict[str, Any]:
    coordinator = require_subagent_coordinator(context)
    context.require_workspace_access(request.workspace_id)
    try:
        task = coordinator.request(subagent_task_request_from_api(request))
        if request.schedule:
            task = require_subagent_lifecycle(context).schedule(task)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"task": subagent_task_to_dict(task), "ok": task.status != "failed"}


def submit_speculative_subagent_task_payload(
    context: SubagentsApiContext,
    request: SpeculativeSubagentTaskRequest,
) -> dict[str, Any]:
    coordinator = require_subagent_coordinator(context)
    lifecycle = require_subagent_lifecycle(context)
    context.require_workspace_access(request.workspace_id)
    try:
        task = coordinator.request_speculative(
            subagent_task_request_from_api(request),
            speculative_key=request.speculative_key,
        )
        task = lifecycle.schedule(task)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"task": subagent_task_to_dict(task), "ok": task.status != "failed"}


async def confirm_speculative_subagent_task_payload(
    context: SubagentsApiContext,
    task_id: str,
    request: SpeculativeSubagentConfirmRequest,
) -> dict[str, Any]:
    coordinator = require_subagent_coordinator(context)
    context.require_workspace_access(request.workspace_id)
    try:
        task = coordinator.confirm_speculative(
            task_id,
            request.workspace_id,
            final_request_event_id=request.final_request_event_id,
            final_input_text=request.final_input_text,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if request.notify_if_terminal and task.status == "completed":
        await context.notify_subagent_terminal_task(task)
    return {"task": subagent_task_to_dict(task), "ok": True}


def cancel_speculative_subagent_task_payload(
    context: SubagentsApiContext,
    task_id: str,
    request: SpeculativeSubagentCancelRequest,
) -> dict[str, Any]:
    coordinator = require_subagent_coordinator(context)
    context.require_workspace_access(request.workspace_id)
    try:
        task = coordinator.cancel_speculative(task_id, request.workspace_id, reason=request.reason)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"task": subagent_task_to_dict(task), "ok": task.metadata.get("speculative_status") == "cancelled"}


def cancel_subagent_task_payload(
    context: SubagentsApiContext,
    task_id: str,
    request: SubagentTaskCancelRequest,
) -> dict[str, Any]:
    lifecycle = require_subagent_lifecycle(context)
    context.require_workspace_access(request.workspace_id)
    try:
        task = lifecycle.mark_terminal(
            require_subagent_coordinator(context).cancel(task_id, request.workspace_id)
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"task": subagent_task_to_dict(task), "ok": task.status == "cancelled"}


def subagent_task_lifecycle_payload(
    context: SubagentsApiContext,
    *,
    workspace_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    lifecycle = require_subagent_lifecycle(context)
    return {"lifecycle": lifecycle.snapshot(workspace_id=workspace_id, session_id=session_id)}


def subagent_task_request_from_api(request: SubagentTaskSubmitRequest) -> SubagentTaskRequest:
    return SubagentTaskRequest(
        workspace_id=request.workspace_id,
        voicebot_id=request.voicebot_id,
        session_id=request.session_id,
        request_event_id=request.request_event_id,
        provider=request.provider,  # type: ignore[arg-type]
        input_text=request.input_text,
        dedupe_key=request.dedupe_key,
        metadata=request.metadata,
    )


def require_subagent_coordinator(context: SubagentsApiContext) -> Any:
    if context.subagent_coordinator is None:
        raise HTTPException(status_code=503, detail="Subagent coordinator is not configured")
    return context.subagent_coordinator


def require_subagent_lifecycle(context: SubagentsApiContext) -> Any:
    if context.subagent_lifecycle is None:
        raise HTTPException(status_code=503, detail="Subagent lifecycle runner is not configured")
    return context.subagent_lifecycle
