from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal


WorkerRole = Literal[
    "media_ingress",
    "session_orchestrator",
    "stt_worker",
    "tts_worker",
    "agent_worker",
    "task_poller",
    "api",
]


@dataclass(frozen=True)
class RoutingKey:
    workspace_id: str
    voicebot_id: str
    session_id: str | None = None
    provider: str | None = None

    def partition_key(self) -> str:
        parts = [self.workspace_id, self.voicebot_id]
        if self.session_id:
            parts.append(self.session_id)
        return ":".join(parts)

    def provider_key(self) -> str:
        return ":".join(part for part in (self.workspace_id, self.voicebot_id, self.provider) if part)


@dataclass(frozen=True)
class QueueBinding:
    role: WorkerRole
    queue: str
    concurrency: int = 1
    max_inflight_per_workspace: int | None = None
    max_inflight_per_voicebot: int | None = None
    max_inflight_per_provider: int | None = None

    def as_dict(self) -> dict:
        return {
            "role": self.role,
            "queue": self.queue,
            "concurrency": self.concurrency,
            "max_inflight_per_workspace": self.max_inflight_per_workspace,
            "max_inflight_per_voicebot": self.max_inflight_per_voicebot,
            "max_inflight_per_provider": self.max_inflight_per_provider,
        }


@dataclass(frozen=True)
class DeploymentTopology:
    queues: tuple[QueueBinding, ...]
    shared_state: tuple[str, ...] = ("flowhunt_db", "redis")
    event_bus: str = "workspace_event_stream"

    def queue_for_role(self, role: WorkerRole) -> QueueBinding:
        for queue in self.queues:
            if queue.role == role:
                return queue
        raise KeyError(f"queue is not configured for role: {role}")

    def as_dict(self) -> dict:
        return {
            "event_bus": self.event_bus,
            "shared_state": list(self.shared_state),
            "queues": [queue.as_dict() for queue in self.queues],
        }


@dataclass(frozen=True)
class WorkerInstance:
    worker_id: str
    role: WorkerRole
    queue: str
    workspace_id: str | None = None
    voicebot_id: str | None = None
    capacity: int = 1
    status: Literal["active", "draining"] = "active"
    last_heartbeat_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def __post_init__(self) -> None:
        if not self.worker_id:
            raise ValueError("worker_id is required")
        if not self.queue:
            raise ValueError("queue is required")
        if self.capacity < 1:
            raise ValueError("capacity must be greater than or equal to 1")

    def as_dict(self) -> dict:
        return {
            "worker_id": self.worker_id,
            "role": self.role,
            "queue": self.queue,
            "workspace_id": self.workspace_id,
            "voicebot_id": self.voicebot_id,
            "capacity": self.capacity,
            "status": self.status,
            "last_heartbeat_at": self.last_heartbeat_at,
        }


class WorkerRegistry:
    def __init__(self, heartbeat_ttl_seconds: float = 30.0) -> None:
        if heartbeat_ttl_seconds <= 0:
            raise ValueError("heartbeat_ttl_seconds must be positive")
        self.heartbeat_ttl_seconds = heartbeat_ttl_seconds
        self._workers: dict[str, WorkerInstance] = {}

    def heartbeat(self, worker: WorkerInstance, now: datetime | None = None) -> WorkerInstance:
        current = now or datetime.now(UTC)
        existing = self._workers.get(worker.worker_id)
        if existing is not None and existing.role != worker.role:
            raise ValueError("cannot move worker instance across roles")
        if existing is not None and existing.queue != worker.queue:
            raise ValueError("cannot move worker instance across queues")
        updated = WorkerInstance(
            worker_id=worker.worker_id,
            role=worker.role,
            queue=worker.queue,
            workspace_id=worker.workspace_id,
            voicebot_id=worker.voicebot_id,
            capacity=worker.capacity,
            status=worker.status,
            last_heartbeat_at=current.isoformat(),
        )
        self._workers[updated.worker_id] = updated
        return updated

    def mark_draining(self, worker_id: str, now: datetime | None = None) -> WorkerInstance:
        worker = self._workers[worker_id]
        return self.heartbeat(
            WorkerInstance(
                worker_id=worker.worker_id,
                role=worker.role,
                queue=worker.queue,
                workspace_id=worker.workspace_id,
                voicebot_id=worker.voicebot_id,
                capacity=worker.capacity,
                status="draining",
                last_heartbeat_at=worker.last_heartbeat_at,
            ),
            now,
        )

    def remove(self, worker_id: str) -> bool:
        return self._workers.pop(worker_id, None) is not None

    def active(
        self,
        role: WorkerRole | None = None,
        workspace_id: str | None = None,
        now: datetime | None = None,
    ) -> tuple[WorkerInstance, ...]:
        current = now or datetime.now(UTC)
        self.expire(current)
        return tuple(
            worker
            for worker in sorted(self._workers.values(), key=lambda item: item.worker_id)
            if worker.status == "active"
            and (role is None or worker.role == role)
            and (workspace_id is None or worker.workspace_id in (None, workspace_id))
        )

    def expire(self, now: datetime | None = None) -> tuple[WorkerInstance, ...]:
        current = now or datetime.now(UTC)
        expired: list[WorkerInstance] = []
        for worker_id, worker in list(self._workers.items()):
            if _parse_time(worker.last_heartbeat_at) + timedelta(seconds=self.heartbeat_ttl_seconds) <= current:
                expired.append(worker)
                self._workers.pop(worker_id, None)
        return tuple(expired)

    def snapshot(self, now: datetime | None = None) -> dict:
        current = now or datetime.now(UTC)
        self.expire(current)
        return {"workers": [worker.as_dict() for worker in sorted(self._workers.values(), key=lambda item: item.worker_id)]}

    def capacity_summary(self, workspace_id: str | None = None, now: datetime | None = None) -> dict:
        workers = self.active(workspace_id=workspace_id, now=now)
        roles: dict[str, dict[str, int]] = {}
        for worker in workers:
            role = roles.setdefault(worker.role, {"workers": 0, "capacity": 0})
            role["workers"] += 1
            role["capacity"] += worker.capacity
        return {
            "workspace_id": workspace_id,
            "roles": dict(sorted(roles.items())),
            "total_workers": len(workers),
            "total_capacity": sum(worker.capacity for worker in workers),
        }


@dataclass
class WorkspaceBackpressure:
    max_inflight: int
    inflight_by_key: dict[str, int] = field(default_factory=dict)

    def acquire(self, key: str) -> bool:
        current = self.inflight_by_key.get(key, 0)
        if current >= self.max_inflight:
            return False
        self.inflight_by_key[key] = current + 1
        return True

    def release(self, key: str) -> None:
        current = self.inflight_by_key.get(key, 0)
        if current <= 1:
            self.inflight_by_key.pop(key, None)
            return
        self.inflight_by_key[key] = current - 1


@dataclass(frozen=True)
class WorkloadProfile:
    workspace_id: str
    voicebot_id: str
    concurrent_sessions: int
    session_id: str | None = None
    stt_provider: str | None = None
    tts_provider: str | None = None
    agent_provider: str | None = None

    def __post_init__(self) -> None:
        if not self.workspace_id:
            raise ValueError("workspace_id is required")
        if not self.voicebot_id:
            raise ValueError("voicebot_id is required")
        if self.concurrent_sessions < 0:
            raise ValueError("concurrent_sessions must be non-negative")


def build_workload_plan(profile: WorkloadProfile, topology: DeploymentTopology | None = None) -> dict:
    topology = topology or default_deployment_topology()
    session_key = RoutingKey(profile.workspace_id, profile.voicebot_id, session_id=profile.session_id)
    provider_by_role = {
        "stt_worker": profile.stt_provider,
        "tts_worker": profile.tts_provider,
        "agent_worker": profile.agent_provider,
    }
    queues = []
    for binding in topology.queues:
        provider = provider_by_role.get(binding.role)
        provider_key = None
        if provider:
            provider_key = RoutingKey(profile.workspace_id, profile.voicebot_id, provider=provider).provider_key()
        queues.append(
            {
                **binding.as_dict(),
                "partition_key": session_key.partition_key(),
                "provider_key": provider_key,
                "workspace_capacity_ok": _capacity_ok(profile.concurrent_sessions, binding.max_inflight_per_workspace),
                "voicebot_capacity_ok": _capacity_ok(profile.concurrent_sessions, binding.max_inflight_per_voicebot),
                "provider_capacity_ok": _capacity_ok(profile.concurrent_sessions, binding.max_inflight_per_provider),
            }
        )
    return {
        "routing": {
            "workspace_id": profile.workspace_id,
            "voicebot_id": profile.voicebot_id,
            "session_id": profile.session_id,
            "partition_key": session_key.partition_key(),
        },
        "concurrent_sessions": profile.concurrent_sessions,
        "event_bus": topology.event_bus,
        "queues": queues,
    }


def _capacity_ok(concurrent_sessions: int, limit: int | None) -> bool | None:
    if limit is None:
        return None
    return concurrent_sessions <= limit


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def default_deployment_topology() -> DeploymentTopology:
    return DeploymentTopology(
        queues=(
            QueueBinding("media_ingress", "voicebot.media", concurrency=1, max_inflight_per_workspace=200),
            QueueBinding("session_orchestrator", "voicebot.sessions", concurrency=4, max_inflight_per_workspace=500),
            QueueBinding("stt_worker", "voicebot.stt", concurrency=8, max_inflight_per_workspace=100, max_inflight_per_provider=50),
            QueueBinding("tts_worker", "voicebot.tts", concurrency=8, max_inflight_per_workspace=100, max_inflight_per_provider=50),
            QueueBinding("agent_worker", "voicebot.agent", concurrency=16, max_inflight_per_workspace=100),
            QueueBinding("task_poller", "voicebot.tasks", concurrency=8, max_inflight_per_workspace=500),
            QueueBinding("api", "voicebot.api", concurrency=4),
        )
    )
