from __future__ import annotations

from typing import Protocol
import base64
import json
from uuid import uuid4

from ..subagents import (
    SubagentTask,
    SubagentTaskRequest,
    SubagentTaskStore,
    subagent_task_from_dict,
    subagent_task_to_dict,
)
from .errors import StorageUnavailable


class RedisSubagentTaskClient(Protocol):
    def get(self, key: str) -> bytes | str | None: ...
    def set(
        self,
        key: str,
        value: str,
        ex: int | None = None,
        px: int | None = None,
        nx: bool = False,
    ) -> object: ...
    def delete(self, *keys: str) -> int: ...
    def keys(self, pattern: str) -> list[bytes | str]: ...
    def ping(self) -> object: ...


class RedisSubagentTaskStore(SubagentTaskStore):
    def __init__(
        self,
        redis_url: str,
        client: RedisSubagentTaskClient | None = None,
        prefix: str = "voicebot:subagent_tasks",
    ) -> None:
        super().__init__()
        self.redis_url = redis_url
        self.prefix = prefix.strip(":") or "voicebot:subagent_tasks"
        self.client = client or _redis_client_from_url(redis_url)
        self.client.ping()

    def get_or_create_requested(self, request: SubagentTaskRequest) -> tuple[SubagentTask, bool]:
        dedupe_key = self._dedupe_key(request.workspace_id, request.effective_dedupe_key)
        existing_id = self.client.get(dedupe_key)
        if existing_id is not None:
            existing = self.get(_decode(existing_id))
            if existing is not None:
                return existing, False
            self.client.delete(dedupe_key)

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
        if self.client.set(dedupe_key, task.task_id, nx=True):
            self.client.set(self._task_key(task.task_id), json.dumps(subagent_task_to_dict(task), sort_keys=True))
            return task, True

        existing_id = self.client.get(dedupe_key)
        existing = self.get(_decode(existing_id)) if existing_id is not None else None
        if existing is not None:
            return existing, False
        self.client.delete(dedupe_key)
        return self.get_or_create_requested(request)

    def update(self, task: SubagentTask) -> SubagentTask:
        existing = self.get(task.task_id)
        if existing is None:
            raise KeyError(f"unknown subagent task: {task.task_id}")
        self._validate_identity(existing, task)
        self.client.set(self._task_key(task.task_id), json.dumps(subagent_task_to_dict(task), sort_keys=True))
        return task

    def get(self, task_id: str, workspace_id: str | None = None) -> SubagentTask | None:
        payload = self.client.get(self._task_key(task_id))
        if payload is None:
            return None
        task = subagent_task_from_dict(json.loads(_decode(payload)))
        if workspace_id is not None and task.workspace_id != workspace_id:
            return None
        return task

    def list(self, workspace_id: str | None = None, session_id: str | None = None) -> list[SubagentTask]:
        tasks: list[SubagentTask] = []
        for key in self.client.keys(f"{self.prefix}:task:*"):
            payload = self.client.get(_decode(key))
            if payload is None:
                continue
            task = subagent_task_from_dict(json.loads(_decode(payload)))
            if workspace_id is not None and task.workspace_id != workspace_id:
                continue
            if session_id is not None and task.session_id != session_id:
                continue
            tasks.append(task)
        return sorted(tasks, key=lambda task: (task.created_at, task.task_id))

    def pending(self) -> list[SubagentTask]:
        return [task for task in self.list() if not task.is_terminal()]

    def _validate_identity(self, existing: SubagentTask, updated: SubagentTask) -> None:
        if existing.workspace_id != updated.workspace_id:
            raise ValueError("cannot move subagent task across workspaces")
        if existing.session_id != updated.session_id:
            raise ValueError("cannot move subagent task across sessions")
        if existing.voicebot_id != updated.voicebot_id:
            raise ValueError("cannot move subagent task across voicebots")
        if existing.provider != updated.provider:
            raise ValueError("cannot move subagent task across providers")
        if existing.request_event_id != updated.request_event_id:
            raise ValueError("cannot move subagent task across request events")

    def _task_key(self, task_id: str) -> str:
        return f"{self.prefix}:task:{_key_part(task_id)}"

    def _dedupe_key(self, workspace_id: str, dedupe_key: str) -> str:
        return f"{self.prefix}:dedupe:{_key_part(workspace_id)}:{_key_part(dedupe_key)}"


def _redis_client_from_url(redis_url: str) -> RedisSubagentTaskClient:
    try:
        import redis
    except ImportError as exc:
        raise StorageUnavailable(
            "redis package is not installed",
            family="subagent_tasks",
            driver="redis",
        ) from exc
    return redis.Redis.from_url(redis_url)


def _decode(value: bytes | str) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else value


def _key_part(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")
