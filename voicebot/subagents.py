from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable
from uuid import uuid4

from .events import EventStore, VoicebotEvent
from .execution_model import ExecutionScope
from .flowhunt import (
    extract_flow_task_error,
    extract_flow_task_result,
    extract_issue_result,
    extract_issue_state,
    is_flow_task_terminal,
    is_terminal_issue_state,
)


SubagentProviderKind = Literal[
    "flowhunt_flow",
    "flowhunt_project",
    "internal_worker",
    "http_service",
    "human_handoff",
]

SubagentTaskStatus = Literal[
    "requested",
    "accepted",
    "running",
    "completed",
    "failed",
    "timed_out",
    "cancelled",
]


@dataclass(frozen=True)
class SubagentProviderDescriptor:
    kind: SubagentProviderKind
    label: str
    workspace_scoped: bool = True
    supports_async_polling: bool = True
    supports_cancel: bool = True
    required_metadata: tuple[str, ...] = ()
    result_context: Literal["clean", "raw"] = "clean"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "workspace_scoped": self.workspace_scoped,
            "supports_async_polling": self.supports_async_polling,
            "supports_cancel": self.supports_cancel,
            "required_metadata": list(self.required_metadata),
            "result_context": self.result_context,
        }


DEFAULT_SUBAGENT_PROVIDER_DESCRIPTORS: dict[SubagentProviderKind, SubagentProviderDescriptor] = {
    "flowhunt_flow": SubagentProviderDescriptor(
        kind="flowhunt_flow",
        label="FlowHunt flow invoke",
        required_metadata=("flow_id",),
    ),
    "flowhunt_project": SubagentProviderDescriptor(
        kind="flowhunt_project",
        label="FlowHunt project issue",
        required_metadata=("project_id",),
    ),
    "internal_worker": SubagentProviderDescriptor(kind="internal_worker", label="Internal worker agent"),
    "http_service": SubagentProviderDescriptor(kind="http_service", label="HTTP/service task provider"),
    "human_handoff": SubagentProviderDescriptor(
        kind="human_handoff",
        label="Human handoff",
        supports_async_polling=False,
    ),
}


@dataclass(frozen=True)
class SubagentTaskRequest:
    workspace_id: str
    session_id: str
    request_event_id: int
    provider: SubagentProviderKind
    input_text: str
    voicebot_id: str | None = None
    dedupe_key: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.workspace_id:
            raise ValueError("workspace_id is required for subagent execution")
        if not self.session_id:
            raise ValueError("session_id is required for subagent execution")
        if self.request_event_id < 1:
            raise ValueError("request_event_id must be positive")

    @property
    def effective_dedupe_key(self) -> str:
        return self.dedupe_key or f"{self.session_id}:{self.request_event_id}"


@dataclass(frozen=True)
class SubagentTaskResult:
    summary: str
    content: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    provider_payload: dict[str, Any] = field(default_factory=dict)

    def clean_context(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "content": self.content,
            "context": self.context,
        }


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class SubagentTask:
    task_id: str
    workspace_id: str
    session_id: str
    request_event_id: int
    provider: SubagentProviderKind
    status: SubagentTaskStatus
    input_text: str
    voicebot_id: str | None = None
    external_task_id: str | None = None
    result: SubagentTaskResult | None = None
    error: str | None = None
    dedupe_key: str = ""
    created_at: str = field(default_factory=_timestamp)
    updated_at: str = field(default_factory=_timestamp)
    progress_messages: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0
    next_poll_at: str | None = None
    deadline_at: str | None = None
    terminal_event_emitted_at: str | None = None
    provider_references: dict[str, Any] = field(default_factory=dict)

    def with_status(
        self,
        status: SubagentTaskStatus,
        *,
        result: SubagentTaskResult | None = None,
        error: str | None = None,
        external_task_id: str | None = None,
        progress_message: str | None = None,
    ) -> "SubagentTask":
        progress = self.progress_messages
        if progress_message and (not progress or progress[-1] != progress_message):
            progress = (*progress, progress_message)
        return replace(
            self,
            status=status,
            result=result if result is not None else self.result,
            error=error,
            external_task_id=external_task_id if external_task_id is not None else self.external_task_id,
            updated_at=_timestamp(),
            progress_messages=progress,
        )

    def with_poll_schedule(
        self,
        *,
        attempts: int | None = None,
        next_poll_at: str | None = None,
        deadline_at: str | None = None,
    ) -> "SubagentTask":
        return replace(
            self,
            attempts=self.attempts if attempts is None else attempts,
            next_poll_at=next_poll_at,
            deadline_at=deadline_at if deadline_at is not None else self.deadline_at,
            updated_at=_timestamp(),
        )

    def with_terminal_event_emitted(self) -> "SubagentTask":
        return replace(self, terminal_event_emitted_at=_timestamp(), updated_at=_timestamp())

    def is_terminal(self) -> bool:
        return self.status in {"completed", "failed", "timed_out", "cancelled"}

    def clean_result_context(self) -> dict[str, Any]:
        if self.result is None:
            return {
                "task_id": self.task_id,
                "status": self.status,
                "provider": self.provider,
                "progress": list(self.progress_messages),
            }
        return {
            "task_id": self.task_id,
            "status": self.status,
            "provider": self.provider,
            **self.result.clean_context(),
        }

    def event_context(self) -> dict[str, Any]:
        data = {
            "task_id": self.task_id,
            "status": self.status,
            "provider": self.provider,
            "request_event_id": self.request_event_id,
            "external_task_id": self.external_task_id,
            "dedupe_key": self.dedupe_key,
            "progress": list(self.progress_messages),
        }
        if self.error:
            data["error"] = self.error
        if self.result is not None:
            data["result"] = self.result.clean_context()
        return {key: value for key, value in data.items() if value not in (None, "", [])}


@runtime_checkable
class SubagentProvider(Protocol):
    kind: SubagentProviderKind

    def submit(self, request: SubagentTaskRequest) -> SubagentTask:
        ...

    def poll(self, task: SubagentTask) -> SubagentTask:
        ...

    def cancel(self, task: SubagentTask) -> SubagentTask:
        ...


class SubagentTaskStore:
    def __init__(self) -> None:
        self._tasks: dict[str, SubagentTask] = {}
        self._dedupe_index: dict[tuple[str, str], str] = {}

    def get_or_create_requested(self, request: SubagentTaskRequest) -> tuple[SubagentTask, bool]:
        key = (request.workspace_id, request.effective_dedupe_key)
        existing_id = self._dedupe_index.get(key)
        if existing_id:
            return self._tasks[existing_id], False

        task = SubagentTask(
            task_id=str(uuid4()),
            workspace_id=request.workspace_id,
            session_id=request.session_id,
            request_event_id=request.request_event_id,
            provider=request.provider,
            status="requested",
            input_text=request.input_text,
            voicebot_id=request.voicebot_id,
            dedupe_key=request.effective_dedupe_key,
            metadata=request.metadata,
        )
        self._tasks[task.task_id] = task
        self._dedupe_index[key] = task.task_id
        return task, True

    def update(self, task: SubagentTask) -> SubagentTask:
        existing = self._tasks.get(task.task_id)
        if existing is None:
            raise KeyError(f"unknown subagent task: {task.task_id}")
        if existing.workspace_id != task.workspace_id:
            raise ValueError("cannot move subagent task across workspaces")
        self._tasks[task.task_id] = task
        return task

    def get(self, task_id: str, workspace_id: str | None = None) -> SubagentTask | None:
        task = self._tasks.get(task_id)
        if task is None:
            return None
        if workspace_id is not None and task.workspace_id != workspace_id:
            return None
        return task

    def list(self, workspace_id: str | None = None, session_id: str | None = None) -> list[SubagentTask]:
        tasks = self._tasks.values()
        if workspace_id is not None:
            tasks = [task for task in tasks if task.workspace_id == workspace_id]
        if session_id is not None:
            tasks = [task for task in tasks if task.session_id == session_id]
        return sorted(tasks, key=lambda task: (task.created_at, task.task_id))

    def pending(self) -> list[SubagentTask]:
        return [task for task in self.list() if not task.is_terminal()]


class JsonSubagentTaskStore(SubagentTaskStore):
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        super().__init__()
        self._load()

    def get_or_create_requested(self, request: SubagentTaskRequest) -> tuple[SubagentTask, bool]:
        task, created = super().get_or_create_requested(request)
        if created:
            self._save()
        return task, created

    def update(self, task: SubagentTask) -> SubagentTask:
        updated = super().update(task)
        self._save()
        return updated

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        for item in raw.get("tasks", []):
            task = subagent_task_from_dict(item)
            self._tasks[task.task_id] = task
            self._dedupe_index[(task.workspace_id, task.dedupe_key)] = task.task_id

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "tasks": [subagent_task_to_dict(task) for task in self.list()]}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True, indent=2))
        tmp.replace(self.path)


class SubagentCoordinator:
    def __init__(self, store: SubagentTaskStore | None = None, events: EventStore | None = None) -> None:
        self.store = store or SubagentTaskStore()
        self.events = events
        self.providers: dict[SubagentProviderKind, SubagentProvider] = {}
        self.provider_descriptors: dict[SubagentProviderKind, SubagentProviderDescriptor] = dict(
            DEFAULT_SUBAGENT_PROVIDER_DESCRIPTORS
        )

    def register(
        self,
        provider: SubagentProvider,
        descriptor: SubagentProviderDescriptor | None = None,
    ) -> None:
        self.providers[provider.kind] = provider
        self.provider_descriptors[provider.kind] = descriptor or DEFAULT_SUBAGENT_PROVIDER_DESCRIPTORS[provider.kind]

    def provider_catalog(self) -> dict[str, Any]:
        return {
            "providers": {
                kind: {
                    **descriptor.to_dict(),
                    "registered": kind in self.providers,
                }
                for kind, descriptor in sorted(self.provider_descriptors.items())
            }
        }

    def provider_descriptor(self, kind: SubagentProviderKind) -> SubagentProviderDescriptor:
        return self.provider_descriptors[kind]

    def supports_cancel(self, kind: SubagentProviderKind) -> bool:
        return self.provider_descriptor(kind).supports_cancel

    def request(self, request: SubagentTaskRequest) -> SubagentTask:
        task, created = self.store.get_or_create_requested(request)
        if not created:
            self._emit_task_event("subagent_task_deduplicated", task)
            return task
        self._emit_task_event("subagent_task_requested", task)
        provider = self._provider(request.provider)
        submitted = provider.submit(request)
        if submitted.workspace_id != request.workspace_id:
            raise ValueError("subagent provider returned task for a different workspace")
        if submitted.task_id != task.task_id:
            submitted = replace(submitted, task_id=task.task_id)
        updated = self.store.update(submitted)
        self._emit_task_event("subagent_task_updated", updated)
        return updated

    def poll(self, task_id: str, workspace_id: str) -> SubagentTask:
        task = self.store.get(task_id, workspace_id)
        if task is None:
            raise KeyError(f"unknown subagent task in workspace {workspace_id}: {task_id}")
        if task.status in {"completed", "failed", "timed_out", "cancelled"}:
            return task
        updated = self._provider(task.provider).poll(task)
        if updated.workspace_id != workspace_id:
            raise ValueError("subagent provider returned task for a different workspace")
        stored = self.store.update(updated)
        self._emit_task_event("subagent_task_updated", stored)
        return stored

    def cancel(self, task_id: str, workspace_id: str) -> SubagentTask:
        task = self.store.get(task_id, workspace_id)
        if task is None:
            raise KeyError(f"unknown subagent task in workspace {workspace_id}: {task_id}")
        updated = self._provider(task.provider).cancel(task)
        if updated.workspace_id != workspace_id:
            raise ValueError("subagent provider returned task for a different workspace")
        stored = self.store.update(updated)
        self._emit_task_event("subagent_task_cancelled", stored)
        return stored

    def _provider(self, kind: SubagentProviderKind) -> SubagentProvider:
        provider = self.providers.get(kind)
        if provider is None:
            raise KeyError(f"subagent provider is not registered: {kind}")
        return provider

    def _emit_task_event(self, event_type: str, task: SubagentTask) -> VoicebotEvent | None:
        if self.events is None:
            return None
        return self.events.append_scoped(
            ExecutionScope(
                workspace_id=task.workspace_id,
                voicebot_id=task.voicebot_id or "",
                session_id=task.session_id,
                call_id=task.session_id,
            ),
            event_type,
            task.event_context(),
        )


class FlowHuntSubagentProvider:
    def __init__(self, kind: Literal["flowhunt_flow", "flowhunt_project"], client: Any, target_id: str) -> None:
        self.kind = kind
        self.client = client
        self.target_id = target_id

    def submit(self, request: SubagentTaskRequest) -> SubagentTask:
        target_id = str(request.metadata.get("flow_id") or request.metadata.get("project_id") or self.target_id)
        if self.kind == "flowhunt_flow":
            result = self.client.invoke_flow_and_wait(target_id, request.input_text, 0, 3)
        else:
            result = self.client.create_project_issue(
                target_id,
                request.input_text[:120] or "Voicebot delegated task",
                request.input_text,
                request.metadata,
            )
        external_task_id = _extract_external_task_id(getattr(result, "data", {}) or {})
        status = "running" if getattr(result, "ok", False) else "failed"
        task = SubagentTask(
            task_id=str(uuid4()),
            workspace_id=request.workspace_id,
            session_id=request.session_id,
            request_event_id=request.request_event_id,
            provider=self.kind,
            status=status,
            input_text=request.input_text,
            voicebot_id=request.voicebot_id,
            external_task_id=external_task_id,
            dedupe_key=request.effective_dedupe_key,
            metadata=request.metadata,
            provider_references={"target_id": target_id, "external_task_id": external_task_id},
        )
        if status == "failed":
            return task.with_status("failed", error=getattr(result, "message", "FlowHunt request failed"))
        if not (getattr(result, "data", {}) or {}).get("pending"):
            completed = self._completed_task_from_result(task, result)
            if completed is not None:
                return completed
        return task.with_status("running", progress_message="A FlowHunt colleague is working on the request.")

    def poll(self, task: SubagentTask) -> SubagentTask:
        target_id = str(task.provider_references.get("target_id") or self.target_id)
        if self.kind == "flowhunt_flow":
            if not task.external_task_id:
                return task.with_status("failed", error="FlowHunt flow task id is missing")
            result = self.client.get_flow_task(target_id, task.external_task_id)
        else:
            result = self.client.get_project_issue(target_id, task.external_task_id)
        if not getattr(result, "ok", False):
            return task.with_status("failed", error=getattr(result, "message", "FlowHunt task failed"))
        data = getattr(result, "data", {}) or {}
        if data.get("pending"):
            return task.with_status("running", progress_message=getattr(result, "message", "Still working."))
        completed = self._completed_task_from_result(task, result)
        if completed is not None:
            return completed
        return task.with_status("running", progress_message=getattr(result, "message", "Still working."))

    def _completed_task_from_result(self, task: SubagentTask, result: Any) -> SubagentTask | None:
        data = getattr(result, "data", {}) or {}
        response = data.get("response") if isinstance(data, dict) else None
        if self.kind == "flowhunt_flow":
            message = extract_flow_task_result(response) or extract_flow_task_result(data)
            if message:
                return _completed_task(task, message, data)
            if is_flow_task_terminal(response) or is_flow_task_terminal(data):
                return task.with_status(
                    "failed",
                    error=extract_flow_task_error(response) or extract_flow_task_error(data) or "FlowHunt flow finished without a result.",
                )
            return None

        message = extract_issue_result(response) or extract_issue_result(data)
        state = extract_issue_state(response).lower()
        if message:
            return _completed_task(task, message, data)
        if state and is_terminal_issue_state(state):
            if state in {"failed", "error", "cancelled", "canceled", "human_input_needed"}:
                return task.with_status("failed", error=getattr(result, "message", "") or f"FlowHunt project issue finished with status {state}.")
            return _completed_task(task, getattr(result, "message", "") or f"FlowHunt project issue finished with status {state}.", data)
        return None

    def cancel(self, task: SubagentTask) -> SubagentTask:
        return task.with_status("cancelled", progress_message="The delegated task was cancelled.")


def _completed_task(task: SubagentTask, message: str, data: dict[str, Any]) -> SubagentTask:
    return task.with_status(
        "completed",
        result=SubagentTaskResult(
            summary=message or "FlowHunt task completed.",
            content=str(message or ""),
            context={"external_task_id": task.external_task_id},
            provider_payload=data,
        ),
    )


def _extract_external_task_id(data: dict[str, Any]) -> str | None:
    for key in ("task_id", "issue_id", "id"):
        value = data.get(key)
        if value:
            return str(value)
    response = data.get("response")
    if isinstance(response, dict):
        return _extract_external_task_id(response)
    return None


def subagent_task_to_dict(task: SubagentTask) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "workspace_id": task.workspace_id,
        "session_id": task.session_id,
        "request_event_id": task.request_event_id,
        "provider": task.provider,
        "status": task.status,
        "input_text": task.input_text,
        "voicebot_id": task.voicebot_id,
        "external_task_id": task.external_task_id,
        "result": subagent_result_to_dict(task.result) if task.result else None,
        "error": task.error,
        "dedupe_key": task.dedupe_key,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "progress_messages": list(task.progress_messages),
        "metadata": task.metadata,
        "attempts": task.attempts,
        "next_poll_at": task.next_poll_at,
        "deadline_at": task.deadline_at,
        "terminal_event_emitted_at": task.terminal_event_emitted_at,
        "provider_references": task.provider_references,
    }


def subagent_task_from_dict(data: dict[str, Any]) -> SubagentTask:
    result_data = data.get("result")
    return SubagentTask(
        task_id=str(data["task_id"]),
        workspace_id=str(data["workspace_id"]),
        session_id=str(data["session_id"]),
        request_event_id=int(data["request_event_id"]),
        provider=data["provider"],
        status=data["status"],
        input_text=str(data.get("input_text", "")),
        voicebot_id=data.get("voicebot_id"),
        external_task_id=data.get("external_task_id"),
        result=subagent_result_from_dict(result_data) if isinstance(result_data, dict) else None,
        error=data.get("error"),
        dedupe_key=str(data.get("dedupe_key", "")),
        created_at=str(data.get("created_at") or _timestamp()),
        updated_at=str(data.get("updated_at") or _timestamp()),
        progress_messages=tuple(str(item) for item in data.get("progress_messages", [])),
        metadata=dict(data.get("metadata") or {}),
        attempts=int(data.get("attempts") or 0),
        next_poll_at=data.get("next_poll_at"),
        deadline_at=data.get("deadline_at"),
        terminal_event_emitted_at=data.get("terminal_event_emitted_at"),
        provider_references=dict(data.get("provider_references") or {}),
    )


def subagent_result_to_dict(result: SubagentTaskResult) -> dict[str, Any]:
    return {
        "summary": result.summary,
        "content": result.content,
        "context": result.context,
        "provider_payload": result.provider_payload,
    }


def subagent_result_from_dict(data: dict[str, Any]) -> SubagentTaskResult:
    return SubagentTaskResult(
        summary=str(data.get("summary", "")),
        content=str(data.get("content", "")),
        context=dict(data.get("context") or {}),
        provider_payload=dict(data.get("provider_payload") or {}),
    )
