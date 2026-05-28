from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal

from .subagents import SubagentCoordinator, SubagentTask


TaskLifecycleEventType = Literal[
    "subagent_task_completed",
    "subagent_task_failed",
    "subagent_task_timed_out",
    "subagent_task_cancelled",
    "subagent_task_late_completed",
]

TaskEventSink = Callable[[TaskLifecycleEventType, SubagentTask], None]
SessionActiveCheck = Callable[[str], bool]


@dataclass(frozen=True)
class PollingPolicy:
    initial_interval_seconds: float = 3.0
    max_interval_seconds: float = 30.0
    backoff_multiplier: float = 2.0
    timeout_seconds: float = 600.0
    max_attempts: int = 100

    def next_interval(self, attempts: int) -> float:
        exponent = max(0, attempts - 1)
        interval = self.initial_interval_seconds * (self.backoff_multiplier**exponent)
        return min(self.max_interval_seconds, interval)


class SubagentTaskLifecycleRunner:
    def __init__(
        self,
        coordinator: SubagentCoordinator,
        *,
        policy: PollingPolicy | None = None,
        event_sink: TaskEventSink | None = None,
        session_active: SessionActiveCheck | None = None,
    ) -> None:
        self.coordinator = coordinator
        self.policy = policy or PollingPolicy()
        self.event_sink = event_sink
        self.session_active = session_active or (lambda _session_id: True)

    def schedule(self, task: SubagentTask, now: datetime | None = None) -> SubagentTask:
        if task.is_terminal():
            return self._mark_terminal(task)
        current = now or _now()
        deadline_at = task.deadline_at or _iso(current + timedelta(seconds=max(0.0, self.policy.timeout_seconds)))
        next_poll_at = task.next_poll_at or _iso(current + timedelta(seconds=max(0.0, self.policy.initial_interval_seconds)))
        return self.coordinator.store.update(
            task.with_poll_schedule(next_poll_at=next_poll_at, deadline_at=deadline_at)
        )

    def tick(self, now: datetime | None = None) -> list[SubagentTask]:
        current = now or _now()
        changed: list[SubagentTask] = []
        for task in self.coordinator.store.pending():
            if self._timed_out(task, current):
                changed.append(self._mark_terminal(task.with_status("timed_out", error="subagent task timed out")))
                continue
            if not self._is_due(task, current):
                continue
            changed.append(self._poll_due_task(task, current))
        return changed

    def cancel_session(self, session_id: str, workspace_id: str) -> list[SubagentTask]:
        cancelled: list[SubagentTask] = []
        for task in self.coordinator.store.list(workspace_id=workspace_id, session_id=session_id):
            if task.is_terminal():
                continue
            cancelled.append(self._mark_terminal(self.coordinator.cancel(task.task_id, workspace_id)))
        return cancelled

    def _poll_due_task(self, task: SubagentTask, now: datetime) -> SubagentTask:
        attempts = task.attempts + 1
        self.coordinator.store.update(task.with_poll_schedule(attempts=attempts))
        try:
            updated = self.coordinator.poll(task.task_id, task.workspace_id)
        except Exception as exc:
            if attempts >= self.policy.max_attempts:
                failed = task.with_status("failed", error=f"provider polling failed after {attempts} attempts: {exc}")
                return self._mark_terminal(self.coordinator.store.update(failed.with_poll_schedule(attempts=attempts)))
            next_poll_at = _iso(now + timedelta(seconds=self.policy.next_interval(attempts)))
            retrying = task.with_status("running", error=f"provider polling failed: {exc}")
            return self.coordinator.store.update(retrying.with_poll_schedule(attempts=attempts, next_poll_at=next_poll_at))

        if updated.is_terminal():
            return self._mark_terminal(updated)
        next_poll_at = _iso(now + timedelta(seconds=self.policy.next_interval(attempts)))
        return self.coordinator.store.update(updated.with_poll_schedule(attempts=attempts, next_poll_at=next_poll_at))

    def _mark_terminal(self, task: SubagentTask) -> SubagentTask:
        stored = self.coordinator.store.update(task)
        if stored.terminal_event_emitted_at:
            return stored
        if self.event_sink is not None:
            self.event_sink(self._terminal_event_type(stored), stored)
        return self.coordinator.store.update(stored.with_terminal_event_emitted())

    def _terminal_event_type(self, task: SubagentTask) -> TaskLifecycleEventType:
        if task.status == "completed" and not self.session_active(task.session_id):
            return "subagent_task_late_completed"
        if task.status == "completed":
            return "subagent_task_completed"
        if task.status == "timed_out":
            return "subagent_task_timed_out"
        if task.status == "cancelled":
            return "subagent_task_cancelled"
        return "subagent_task_failed"

    def _is_due(self, task: SubagentTask, now: datetime) -> bool:
        if not task.next_poll_at:
            return True
        return _parse_time(task.next_poll_at) <= now

    def _timed_out(self, task: SubagentTask, now: datetime) -> bool:
        return bool(task.deadline_at and _parse_time(task.deadline_at) <= now)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
