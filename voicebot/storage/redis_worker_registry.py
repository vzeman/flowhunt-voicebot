from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol
import json
import math

from ..scaling import WorkerInstance, WorkerRegistry, WorkerRole, worker_instance_from_dict
from .errors import StorageUnavailable


class RedisWorkerRegistryClient(Protocol):
    def get(self, key: str) -> bytes | str | None: ...
    def set(self, key: str, value: str, ex: int | None = None) -> object: ...
    def delete(self, *keys: str) -> int: ...
    def keys(self, pattern: str) -> list[bytes | str]: ...
    def ping(self) -> object: ...


class RedisWorkerRegistry(WorkerRegistry):
    def __init__(
        self,
        redis_url: str,
        heartbeat_ttl_seconds: float = 30.0,
        client: RedisWorkerRegistryClient | None = None,
        prefix: str = "voicebot:worker_registry",
    ) -> None:
        super().__init__(heartbeat_ttl_seconds=heartbeat_ttl_seconds)
        self.redis_url = redis_url
        self.prefix = prefix.strip(":") or "voicebot:worker_registry"
        self.client = client or _redis_client_from_url(redis_url)
        self.client.ping()

    def heartbeat(self, worker: WorkerInstance, now: datetime | None = None) -> WorkerInstance:
        current = now or datetime.now(UTC)
        existing = self._get(worker.worker_id)
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
        self.client.set(
            self._key(updated.worker_id),
            json.dumps(updated.as_dict(), sort_keys=True),
            ex=max(1, math.ceil(self.heartbeat_ttl_seconds)),
        )
        return updated

    def mark_draining(self, worker_id: str, now: datetime | None = None) -> WorkerInstance:
        worker = self._get(worker_id)
        if worker is None:
            raise KeyError(worker_id)
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
        return self.client.delete(self._key(worker_id)) > 0

    def active(
        self,
        role: WorkerRole | None = None,
        workspace_id: str | None = None,
        voicebot_id: str | None = None,
        now: datetime | None = None,
    ) -> tuple[WorkerInstance, ...]:
        _ = now
        return tuple(
            worker
            for worker in self._workers_from_redis()
            if worker.status == "active"
            and (role is None or worker.role == role)
            and (workspace_id is None or worker.workspace_id in (None, workspace_id))
            and (voicebot_id is None or worker.voicebot_id in (None, voicebot_id))
        )

    def expire(self, now: datetime | None = None) -> tuple[WorkerInstance, ...]:
        _ = now
        return ()

    def snapshot(self, now: datetime | None = None) -> dict:
        _ = now
        return {"workers": [worker.as_dict() for worker in self._workers_from_redis()]}

    def _workers_from_redis(self) -> tuple[WorkerInstance, ...]:
        workers = []
        for key in self.client.keys(f"{self.prefix}:*"):
            worker = self._get_by_key(_decode(key))
            if worker is not None:
                workers.append(worker)
        return tuple(sorted(workers, key=lambda item: item.worker_id))

    def _get(self, worker_id: str) -> WorkerInstance | None:
        return self._get_by_key(self._key(worker_id))

    def _get_by_key(self, key: str) -> WorkerInstance | None:
        payload = self.client.get(key)
        if payload is None:
            return None
        return worker_instance_from_dict(json.loads(_decode(payload)))

    def _key(self, worker_id: str) -> str:
        return f"{self.prefix}:{worker_id}"


def _redis_client_from_url(redis_url: str) -> RedisWorkerRegistryClient:
    try:
        import redis
    except ImportError as exc:
        raise StorageUnavailable(
            "redis package is not installed",
            family="worker_registry",
            driver="redis",
        ) from exc
    return redis.Redis.from_url(redis_url)


def _decode(value: bytes | str) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else value
