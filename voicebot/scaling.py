from __future__ import annotations

from dataclasses import dataclass, field
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
            "queues": [
                {
                    "role": queue.role,
                    "queue": queue.queue,
                    "concurrency": queue.concurrency,
                    "max_inflight_per_workspace": queue.max_inflight_per_workspace,
                    "max_inflight_per_voicebot": queue.max_inflight_per_voicebot,
                    "max_inflight_per_provider": queue.max_inflight_per_provider,
                }
                for queue in self.queues
            ],
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
