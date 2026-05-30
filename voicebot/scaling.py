from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from typing import Any, Literal, get_args


WorkerRole = Literal[
    "media_ingress",
    "session_orchestrator",
    "stt_worker",
    "tts_worker",
    "agent_worker",
    "task_poller",
    "api",
]

WorkItemKind = Literal[
    "media_frame",
    "stt_turn",
    "agent_turn",
    "tts_request",
    "external_task_poll",
    "session_event",
    "summary",
    "post_call",
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
        voicebot_id: str | None = None,
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
            and (voicebot_id is None or worker.voicebot_id in (None, voicebot_id))
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

    def capacity_summary(
        self,
        workspace_id: str | None = None,
        voicebot_id: str | None = None,
        now: datetime | None = None,
    ) -> dict:
        workers = self.active(workspace_id=workspace_id, voicebot_id=voicebot_id, now=now)
        roles: dict[str, dict[str, int]] = {}
        for worker in workers:
            role = roles.setdefault(worker.role, {"workers": 0, "capacity": 0})
            role["workers"] += 1
            role["capacity"] += worker.capacity
        return {
            "workspace_id": workspace_id,
            "voicebot_id": voicebot_id,
            "roles": dict(sorted(roles.items())),
            "total_workers": len(workers),
            "total_capacity": sum(worker.capacity for worker in workers),
        }


class JsonWorkerRegistry(WorkerRegistry):
    def __init__(self, path: str | Path, heartbeat_ttl_seconds: float = 30.0) -> None:
        self.path = Path(path)
        self.load_diagnostics: dict[str, int] = {
            "loaded_workers": 0,
            "skipped_malformed_json": 0,
            "skipped_invalid_workers": 0,
            "skipped_duplicate_worker_ids": 0,
            "skipped_expired_workers": 0,
        }
        super().__init__(heartbeat_ttl_seconds=heartbeat_ttl_seconds)
        self._load()

    def heartbeat(self, worker: WorkerInstance, now: datetime | None = None) -> WorkerInstance:
        updated = super().heartbeat(worker, now=now)
        self._save()
        return updated

    def mark_draining(self, worker_id: str, now: datetime | None = None) -> WorkerInstance:
        worker = super().mark_draining(worker_id, now=now)
        self._save()
        return worker

    def remove(self, worker_id: str) -> bool:
        removed = super().remove(worker_id)
        if removed:
            self._save()
        return removed

    def expire(self, now: datetime | None = None) -> tuple[WorkerInstance, ...]:
        expired = super().expire(now)
        if expired:
            self._save()
        return expired

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.load_diagnostics["skipped_malformed_json"] += 1
            return
        seen: set[str] = set()
        now = datetime.now(UTC)
        for item in payload.get("workers", []):
            try:
                worker = worker_instance_from_dict(item)
            except (KeyError, TypeError, ValueError):
                self.load_diagnostics["skipped_invalid_workers"] += 1
                continue
            if worker.worker_id in seen:
                self.load_diagnostics["skipped_duplicate_worker_ids"] += 1
                continue
            seen.add(worker.worker_id)
            if _parse_time(worker.last_heartbeat_at) + timedelta(seconds=self.heartbeat_ttl_seconds) <= now:
                self.load_diagnostics["skipped_expired_workers"] += 1
                continue
            self._workers[worker.worker_id] = worker
            self.load_diagnostics["loaded_workers"] += 1

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "heartbeat_ttl_seconds": self.heartbeat_ttl_seconds,
            "workers": [worker.as_dict() for worker in sorted(self._workers.values(), key=lambda item: item.worker_id)],
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
        tmp.replace(self.path)


@dataclass
class WorkspaceBackpressure:
    max_inflight: int
    inflight_by_key: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.max_inflight < 1:
            raise ValueError("max_inflight must be greater than or equal to 1")

    def acquire(self, key: str) -> bool:
        if not key.strip():
            raise ValueError("backpressure key is required")
        current = self.inflight_by_key.get(key, 0)
        if current >= self.max_inflight:
            return False
        self.inflight_by_key[key] = current + 1
        return True

    def release(self, key: str) -> None:
        if not key.strip():
            raise ValueError("backpressure key is required")
        current = self.inflight_by_key.get(key, 0)
        if current <= 1:
            self.inflight_by_key.pop(key, None)
            return
        self.inflight_by_key[key] = current - 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "max_inflight": self.max_inflight,
            "inflight": dict(sorted(self.inflight_by_key.items())),
        }


@dataclass(frozen=True)
class WorkerQueueEnvelope:
    item_id: str
    kind: WorkItemKind
    routing: RoutingKey
    queue: str
    payload: dict[str, Any] = field(default_factory=dict)
    trace_id: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    attempt: int = 0
    idempotency_key: str | None = None
    max_attempts: int = 3
    last_error: str | None = None
    failed_at: str | None = None

    def __post_init__(self) -> None:
        if not self.item_id.strip():
            raise ValueError("item_id is required")
        if self.idempotency_key is None:
            object.__setattr__(self, "idempotency_key", self.item_id)
        if not str(self.idempotency_key).strip():
            raise ValueError("idempotency_key is required")
        if self.kind not in get_args(WorkItemKind):
            raise ValueError(f"unsupported work item kind: {self.kind}")
        if not self.queue.strip():
            raise ValueError("queue is required")
        if self.attempt < 0:
            raise ValueError("attempt must be greater than or equal to 0")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be greater than or equal to 1")
        _parse_time(self.created_at)
        if self.failed_at is not None:
            _parse_time(self.failed_at)

    def partition_key(self) -> str:
        return self.routing.partition_key()

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "kind": self.kind,
            "routing": {
                "workspace_id": self.routing.workspace_id,
                "voicebot_id": self.routing.voicebot_id,
                "session_id": self.routing.session_id,
                "provider": self.routing.provider,
                "partition_key": self.routing.partition_key(),
                "provider_key": self.routing.provider_key(),
            },
            "queue": self.queue,
            "payload": self.payload,
            "trace_id": self.trace_id,
            "created_at": self.created_at,
            "attempt": self.attempt,
            "idempotency_key": self.idempotency_key,
            "max_attempts": self.max_attempts,
            "last_error": self.last_error,
            "failed_at": self.failed_at,
        }


class WorkerQueueStore:
    def __init__(self) -> None:
        self._pending: dict[str, deque[WorkerQueueEnvelope]] = {}
        self._claimed: dict[str, tuple[WorkerQueueEnvelope, str, datetime]] = {}
        self._dead_letter: dict[str, WorkerQueueEnvelope] = {}
        self._known_item_ids: set[str] = set()
        self._known_idempotency_keys: dict[str, str] = {}

    def enqueue(self, envelope: WorkerQueueEnvelope) -> WorkerQueueEnvelope:
        if envelope.item_id in self._known_item_ids:
            raise ValueError(f"work item already exists: {envelope.item_id}")
        existing_item_id = self._known_idempotency_keys.get(envelope.idempotency_key or envelope.item_id)
        if existing_item_id is not None:
            existing = self.get(existing_item_id)
            if existing is not None:
                return existing
        self._pending.setdefault(envelope.queue, deque()).append(envelope)
        self._known_item_ids.add(envelope.item_id)
        self._known_idempotency_keys[envelope.idempotency_key or envelope.item_id] = envelope.item_id
        return envelope

    def claim(
        self,
        queue: str,
        owner: str,
        *,
        limit: int = 1,
        ttl_seconds: float = 30.0,
        now: datetime | None = None,
    ) -> tuple[WorkerQueueEnvelope, ...]:
        if not queue.strip():
            raise ValueError("queue is required")
        if not owner.strip():
            raise ValueError("owner is required")
        if limit < 1:
            raise ValueError("limit must be greater than or equal to 1")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        current = now or datetime.now(UTC)
        self.expire(current)
        pending = self._pending.setdefault(queue, deque())
        claimed: list[WorkerQueueEnvelope] = []
        while pending and len(claimed) < limit:
            envelope = pending.popleft()
            updated = replace(envelope, attempt=envelope.attempt + 1)
            self._claimed[updated.item_id] = (updated, owner, current + timedelta(seconds=ttl_seconds))
            claimed.append(updated)
        return tuple(claimed)

    def renew(self, item_id: str, owner: str, ttl_seconds: float = 30.0, now: datetime | None = None) -> WorkerQueueEnvelope | None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        claim = self._claimed.get(item_id)
        if claim is None:
            return None
        envelope, claim_owner, _expires_at = claim
        if claim_owner != owner:
            return None
        current = now or datetime.now(UTC)
        self._claimed[item_id] = (envelope, claim_owner, current + timedelta(seconds=ttl_seconds))
        return envelope

    def ack(self, item_id: str, owner: str | None = None) -> WorkerQueueEnvelope | None:
        claim = self._claimed.get(item_id)
        if claim is None:
            return None
        envelope, claim_owner, _expires_at = claim
        if owner is not None and claim_owner != owner:
            return None
        self._claimed.pop(item_id, None)
        self._known_item_ids.discard(item_id)
        if envelope.idempotency_key is not None:
            self._known_idempotency_keys.pop(envelope.idempotency_key, None)
        return envelope

    def release(self, item_id: str, owner: str | None = None, error: str | None = None) -> WorkerQueueEnvelope | None:
        claim = self._claimed.get(item_id)
        if claim is None:
            return None
        envelope, claim_owner, _expires_at = claim
        if owner is not None and claim_owner != owner:
            return None
        self._claimed.pop(item_id, None)
        updated = replace(envelope, last_error=error or envelope.last_error)
        if updated.attempt >= updated.max_attempts:
            failed = replace(updated, failed_at=datetime.now(UTC).isoformat())
            self._dead_letter[failed.item_id] = failed
            return failed
        self._pending.setdefault(updated.queue, deque()).appendleft(updated)
        return updated

    def expire(self, now: datetime | None = None) -> tuple[WorkerQueueEnvelope, ...]:
        current = now or datetime.now(UTC)
        expired: list[WorkerQueueEnvelope] = []
        for item_id, (envelope, _owner, expires_at) in list(self._claimed.items()):
            if expires_at > current:
                continue
            self._claimed.pop(item_id, None)
            if envelope.attempt >= envelope.max_attempts:
                failed = replace(envelope, last_error="claim expired", failed_at=current.isoformat())
                self._dead_letter[failed.item_id] = failed
                expired.append(failed)
                continue
            self._pending.setdefault(envelope.queue, deque()).appendleft(envelope)
            expired.append(envelope)
        return tuple(expired)

    def get(self, item_id: str) -> WorkerQueueEnvelope | None:
        claim = self._claimed.get(item_id)
        if claim is not None:
            return claim[0]
        if item_id in self._dead_letter:
            return self._dead_letter[item_id]
        for queue in self._pending.values():
            for envelope in queue:
                if envelope.item_id == item_id:
                    return envelope
        return None

    def pending(self, queue: str | None = None) -> tuple[WorkerQueueEnvelope, ...]:
        if queue is not None:
            return tuple(self._pending.get(queue, ()))
        items: list[WorkerQueueEnvelope] = []
        for queue_name in sorted(self._pending):
            items.extend(self._pending[queue_name])
        return tuple(items)

    def claimed(self, owner: str | None = None, now: datetime | None = None) -> tuple[dict[str, Any], ...]:
        current = now or datetime.now(UTC)
        self.expire(current)
        claims = []
        for envelope, claim_owner, expires_at in self._claimed.values():
            if owner is not None and claim_owner != owner:
                continue
            claims.append(
                {
                    "item": envelope.as_dict(),
                    "owner": claim_owner,
                    "expires_in_seconds": max(0.0, (expires_at - current).total_seconds()),
                }
            )
        return tuple(sorted(claims, key=lambda item: item["item"]["item_id"]))

    def dead_lettered(self) -> tuple[WorkerQueueEnvelope, ...]:
        return tuple(self._dead_letter[item_id] for item_id in sorted(self._dead_letter))

    def snapshot(self, now: datetime | None = None) -> dict[str, Any]:
        current = now or datetime.now(UTC)
        self.expire(current)
        return {
            "pending": {
                queue: [envelope.as_dict() for envelope in envelopes]
                for queue, envelopes in sorted(self._pending.items())
                if envelopes
            },
            "claimed": list(self.claimed(now=current)),
            "dead_lettered": [envelope.as_dict() for envelope in self.dead_lettered()],
        }


class JsonWorkerQueueStore(WorkerQueueStore):
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.load_diagnostics: dict[str, int] = {
            "loaded_pending": 0,
            "loaded_claimed": 0,
            "loaded_dead_lettered": 0,
            "requeued_expired_claims": 0,
            "skipped_malformed_json": 0,
            "skipped_invalid_items": 0,
            "skipped_duplicate_item_ids": 0,
        }
        super().__init__()
        self._load()

    def enqueue(self, envelope: WorkerQueueEnvelope) -> WorkerQueueEnvelope:
        item = super().enqueue(envelope)
        self._save()
        return item

    def claim(
        self,
        queue: str,
        owner: str,
        *,
        limit: int = 1,
        ttl_seconds: float = 30.0,
        now: datetime | None = None,
    ) -> tuple[WorkerQueueEnvelope, ...]:
        claimed = super().claim(queue, owner, limit=limit, ttl_seconds=ttl_seconds, now=now)
        self._save()
        return claimed

    def renew(self, item_id: str, owner: str, ttl_seconds: float = 30.0, now: datetime | None = None) -> WorkerQueueEnvelope | None:
        item = super().renew(item_id, owner, ttl_seconds=ttl_seconds, now=now)
        if item is not None:
            self._save()
        return item

    def ack(self, item_id: str, owner: str | None = None) -> WorkerQueueEnvelope | None:
        item = super().ack(item_id, owner=owner)
        self._save()
        return item

    def release(self, item_id: str, owner: str | None = None, error: str | None = None) -> WorkerQueueEnvelope | None:
        item = super().release(item_id, owner=owner, error=error)
        self._save()
        return item

    def expire(self, now: datetime | None = None) -> tuple[WorkerQueueEnvelope, ...]:
        expired = super().expire(now)
        if expired:
            self._save()
        return expired

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.load_diagnostics["skipped_malformed_json"] += 1
            return
        seen: set[str] = set()
        for queue, items in (payload.get("pending") or {}).items():
            if not isinstance(items, list):
                self.load_diagnostics["skipped_invalid_items"] += 1
                continue
            for item in items:
                envelope = self._load_envelope(item, seen)
                if envelope is None:
                    continue
                self._pending.setdefault(str(queue), deque()).append(envelope)
            self._known_item_ids.add(envelope.item_id)
            if envelope.idempotency_key is not None:
                self._known_idempotency_keys[envelope.idempotency_key] = envelope.item_id
            self.load_diagnostics["loaded_pending"] += 1
        now = datetime.now(UTC)
        for claim in payload.get("claimed") or []:
            try:
                envelope = self._load_envelope(claim["item"], seen)
                owner = str(claim["owner"])
                expires_at = _parse_time(str(claim["expires_at"]))
            except (KeyError, TypeError, ValueError):
                self.load_diagnostics["skipped_invalid_items"] += 1
                continue
            if envelope is None:
                continue
            if expires_at <= now:
                if envelope.attempt >= envelope.max_attempts:
                    self._dead_letter[envelope.item_id] = replace(
                        envelope,
                        last_error=envelope.last_error or "claim expired on reload",
                        failed_at=now.isoformat(),
                    )
                    self.load_diagnostics["loaded_dead_lettered"] += 1
                else:
                    self._pending.setdefault(envelope.queue, deque()).append(envelope)
                    self.load_diagnostics["requeued_expired_claims"] += 1
            else:
                self._claimed[envelope.item_id] = (envelope, owner, expires_at)
                self.load_diagnostics["loaded_claimed"] += 1
            self._known_item_ids.add(envelope.item_id)
            if envelope.idempotency_key is not None:
                self._known_idempotency_keys[envelope.idempotency_key] = envelope.item_id
        for item in payload.get("dead_lettered") or []:
            envelope = self._load_envelope(item, seen)
            if envelope is None:
                continue
            self._dead_letter[envelope.item_id] = envelope
            self._known_item_ids.add(envelope.item_id)
            if envelope.idempotency_key is not None:
                self._known_idempotency_keys[envelope.idempotency_key] = envelope.item_id
            self.load_diagnostics["loaded_dead_lettered"] += 1

    def _load_envelope(self, data: dict[str, Any], seen: set[str]) -> WorkerQueueEnvelope | None:
        try:
            envelope = worker_queue_envelope_from_dict(data)
        except (KeyError, TypeError, ValueError):
            self.load_diagnostics["skipped_invalid_items"] += 1
            return None
        if envelope.item_id in seen:
            self.load_diagnostics["skipped_duplicate_item_ids"] += 1
            return None
        seen.add(envelope.item_id)
        return envelope

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "pending": {
                queue: [envelope.as_dict() for envelope in envelopes]
                for queue, envelopes in sorted(self._pending.items())
                if envelopes
            },
            "claimed": [
                {
                    "item": envelope.as_dict(),
                    "owner": owner,
                    "expires_at": expires_at.isoformat(),
                }
                for envelope, owner, expires_at in sorted(
                    self._claimed.values(),
                    key=lambda claim: claim[0].item_id,
                )
            ],
            "dead_lettered": [envelope.as_dict() for envelope in self.dead_lettered()],
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
        tmp.replace(self.path)


def worker_queue_envelope_from_dict(data: dict[str, Any]) -> WorkerQueueEnvelope:
    routing = data["routing"]
    return WorkerQueueEnvelope(
        item_id=str(data["item_id"]),
        kind=str(data["kind"]),  # type: ignore[arg-type]
        routing=RoutingKey(
            workspace_id=str(routing["workspace_id"]),
            voicebot_id=str(routing["voicebot_id"]),
            session_id=_optional_str(routing.get("session_id")),
            provider=_optional_str(routing.get("provider")),
        ),
        queue=str(data["queue"]),
        payload=dict(data.get("payload") or {}),
        trace_id=_optional_str(data.get("trace_id")),
        created_at=str(data["created_at"]),
        attempt=int(data.get("attempt") or 0),
        idempotency_key=_optional_str(data.get("idempotency_key")),
        max_attempts=int(data.get("max_attempts") or 3),
        last_error=_optional_str(data.get("last_error")),
        failed_at=_optional_str(data.get("failed_at")),
    )


def worker_instance_from_dict(data: dict[str, Any]) -> WorkerInstance:
    role = str(data["role"])
    if role not in get_args(WorkerRole):
        raise ValueError(f"unsupported worker role: {role}")
    status = str(data.get("status", "active"))
    if status not in {"active", "draining"}:
        raise ValueError(f"unsupported worker status: {status}")
    _parse_time(str(data["last_heartbeat_at"]))
    return WorkerInstance(
        worker_id=str(data["worker_id"]),
        role=role,  # type: ignore[arg-type]
        queue=str(data["queue"]),
        workspace_id=_optional_str(data.get("workspace_id")),
        voicebot_id=_optional_str(data.get("voicebot_id")),
        capacity=int(data.get("capacity") or 1),
        status=status,  # type: ignore[arg-type]
        last_heartbeat_at=str(data["last_heartbeat_at"]),
    )


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


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


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
