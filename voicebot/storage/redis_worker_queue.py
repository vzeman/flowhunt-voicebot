from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Any, Protocol
import json

from ..scaling import WorkerQueueEnvelope, WorkerQueueStore, worker_queue_envelope_from_dict
from .errors import StorageUnavailable


class RedisWorkerQueueClient(Protocol):
    def get(self, key: str) -> bytes | str | None: ...
    def set(self, key: str, value: str, ex: int | None = None) -> object: ...
    def delete(self, *keys: str) -> int: ...
    def ping(self) -> object: ...


class RedisWorkerQueueStore(WorkerQueueStore):
    def __init__(
        self,
        redis_url: str,
        client: RedisWorkerQueueClient | None = None,
        key: str = "voicebot:worker_queue",
    ) -> None:
        super().__init__()
        self.redis_url = redis_url
        self.key = key
        self.client = client or _redis_client_from_url(redis_url)
        self.client.ping()
        self.load_diagnostics: dict[str, int] = {
            "loaded_pending": 0,
            "loaded_claimed": 0,
            "loaded_dead_lettered": 0,
            "skipped_malformed_json": 0,
            "skipped_invalid_items": 0,
            "skipped_duplicate_item_ids": 0,
        }
        self._inside_loaded_operation = False

    def enqueue(self, envelope: WorkerQueueEnvelope) -> WorkerQueueEnvelope:
        self._reload()
        self._inside_loaded_operation = True
        try:
            item = super().enqueue(envelope)
        finally:
            self._inside_loaded_operation = False
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
        self._reload()
        self._inside_loaded_operation = True
        try:
            claimed = super().claim(queue, owner, limit=limit, ttl_seconds=ttl_seconds, now=now)
        finally:
            self._inside_loaded_operation = False
        self._save()
        return claimed

    def renew(self, item_id: str, owner: str, ttl_seconds: float = 30.0, now: datetime | None = None) -> WorkerQueueEnvelope | None:
        self._reload()
        item = super().renew(item_id, owner, ttl_seconds=ttl_seconds, now=now)
        if item is not None:
            self._save()
        return item

    def ack(self, item_id: str, owner: str | None = None) -> WorkerQueueEnvelope | None:
        self._reload()
        item = super().ack(item_id, owner=owner)
        self._save()
        return item

    def release(self, item_id: str, owner: str | None = None, error: str | None = None) -> WorkerQueueEnvelope | None:
        self._reload()
        item = super().release(item_id, owner=owner, error=error)
        if item is not None:
            self._save()
        return item

    def expire(self, now: datetime | None = None) -> tuple[WorkerQueueEnvelope, ...]:
        if not self._inside_loaded_operation:
            self._reload()
        expired = super().expire(now)
        if expired and not self._inside_loaded_operation:
            self._save()
        return expired

    def get(self, item_id: str) -> WorkerQueueEnvelope | None:
        self._reload()
        return super().get(item_id)

    def pending(self, queue: str | None = None) -> tuple[WorkerQueueEnvelope, ...]:
        self._reload()
        return super().pending(queue)

    def claimed(self, owner: str | None = None, now: datetime | None = None) -> tuple[dict[str, Any], ...]:
        self._reload()
        return super().claimed(owner=owner, now=now)

    def dead_lettered(self) -> tuple[WorkerQueueEnvelope, ...]:
        self._reload()
        return super().dead_lettered()

    def snapshot(self, now: datetime | None = None) -> dict[str, Any]:
        self._reload()
        return super().snapshot(now=now)

    def _reload(self) -> None:
        self._pending = {}
        self._claimed = {}
        self._dead_letter = {}
        self._known_item_ids = set()
        self._known_idempotency_keys = {}
        self.load_diagnostics = {key: 0 for key in self.load_diagnostics}
        raw = self.client.get(self.key)
        if raw is None:
            return
        try:
            payload = json.loads(_decode(raw))
        except (TypeError, json.JSONDecodeError):
            self.load_diagnostics["skipped_malformed_json"] += 1
            return
        if not isinstance(payload, dict):
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
                self._remember(envelope)
                self.load_diagnostics["loaded_pending"] += 1
        for claim in payload.get("claimed") or []:
            try:
                envelope = self._load_envelope(claim["item"], seen)
                owner = str(claim["owner"])
                expires_at = _parse_datetime(str(claim["expires_at"]))
            except (KeyError, TypeError, ValueError):
                self.load_diagnostics["skipped_invalid_items"] += 1
                continue
            if envelope is None:
                continue
            self._claimed[envelope.item_id] = (envelope, owner, expires_at)
            self._remember(envelope)
            self.load_diagnostics["loaded_claimed"] += 1
        for item in payload.get("dead_lettered") or []:
            envelope = self._load_envelope(item, seen)
            if envelope is None:
                continue
            self._dead_letter[envelope.item_id] = envelope
            self._remember(envelope)
            self.load_diagnostics["loaded_dead_lettered"] += 1

    def _load_envelope(self, data: Any, seen: set[str]) -> WorkerQueueEnvelope | None:
        if not isinstance(data, dict):
            self.load_diagnostics["skipped_invalid_items"] += 1
            return None
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

    def _remember(self, envelope: WorkerQueueEnvelope) -> None:
        self._known_item_ids.add(envelope.item_id)
        self._known_idempotency_keys[envelope.idempotency_key or envelope.item_id] = envelope.item_id

    def _save(self) -> None:
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
            "dead_lettered": [envelope.as_dict() for envelope in super().dead_lettered()],
        }
        self.client.set(self.key, json.dumps(payload, sort_keys=True))


def _redis_client_from_url(redis_url: str) -> RedisWorkerQueueClient:
    try:
        import redis
    except ImportError as exc:
        raise StorageUnavailable(
            "redis package is not installed",
            family="worker_queue",
            driver="redis",
        ) from exc
    return redis.Redis.from_url(redis_url)


def _decode(value: bytes | str) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else value


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)
