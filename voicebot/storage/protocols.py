from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .drivers import StoreHealth


@runtime_checkable
class StorageProtocol(Protocol):
    def storage_health(self) -> StoreHealth:
        ...


@runtime_checkable
class EventStoreProtocol(Protocol):
    def append(self, call_id: str, event_type: str, data: dict[str, Any] | None = None) -> Any:
        ...

    def list_events(self, **filters: Any) -> list[Any]:
        ...

    def get_event(self, event_id: int) -> Any | None:
        ...


@runtime_checkable
class TranscriptStoreProtocol(Protocol):
    def append(self, event: Any) -> None:
        ...

    def read(self, call_id: str, after: int = 0, limit: int | None = None) -> list[dict[str, Any]]:
        ...

    def summaries(self, after_call_id: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        ...


@runtime_checkable
class VoicebotSessionStoreProtocol(Protocol):
    def upsert(self, session: Any) -> Any:
        ...

    def get(self, session_id: str) -> Any | None:
        ...


@runtime_checkable
class SessionLeaseStoreProtocol(Protocol):
    def acquire(self, lease: Any, now: Any | None = None) -> bool:
        ...

    def renew(self, key: Any, owner: str, ttl_seconds: float, now: Any | None = None) -> bool:
        ...

    def release(self, key: Any, owner: str) -> bool:
        ...


@runtime_checkable
class AgentTaskStoreProtocol(Protocol):
    def claim(self, event_id: int, owner: str, ttl_seconds: float) -> bool:
        ...

    def mark_responded(self, event_id: int) -> None:
        ...

    def snapshot(self, owner: str | None = None) -> dict[str, Any]:
        ...


@runtime_checkable
class WorkerQueueStoreProtocol(Protocol):
    def submit(self, item: Any) -> Any:
        ...

    def claim(self, queue: str, owner: str, ttl_seconds: float, now: Any | None = None) -> Any | None:
        ...

    def ack(self, item_id: str, owner: str) -> bool:
        ...


@runtime_checkable
class WorkerRegistryStoreProtocol(Protocol):
    def heartbeat(self, worker: Any, now: Any | None = None) -> Any:
        ...

    def snapshot(self, now: Any | None = None) -> dict[str, Any]:
        ...


@runtime_checkable
class CallStateStoreProtocol(Protocol):
    def upsert(self, state: Any) -> Any:
        ...

    def get(self, call_id: str) -> Any | None:
        ...


@runtime_checkable
class ProviderConfigStoreProtocol(Protocol):
    def get_active(self, workspace_id: str, voicebot_id: str) -> dict[str, Any] | None:
        ...


@runtime_checkable
class SipTrunkStoreProtocol(Protocol):
    def list(self) -> list[Any]:
        ...

    def get(self, trunk_id: str) -> Any | None:
        ...

    def upsert(self, trunk: Any) -> Any:
        ...


@runtime_checkable
class SubagentTaskStoreProtocol(Protocol):
    def create(self, task: Any) -> Any:
        ...

    def get(self, task_id: str) -> Any | None:
        ...

    def list(self, **filters: Any) -> list[Any]:
        ...


@runtime_checkable
class ArtifactStoreProtocol(Protocol):
    def put(self, artifact_id: str, data: bytes, metadata: dict[str, Any] | None = None) -> Any:
        ...

    def get(self, artifact_id: str) -> bytes | None:
        ...

    def delete(self, artifact_id: str) -> bool:
        ...
