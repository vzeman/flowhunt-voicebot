from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
import json

from ..session_leases import SessionLease, SessionLeaseStore, lease_key, session_lease_from_dict
from .errors import StorageUnavailable


class RedisLeaseClient(Protocol):
    def get(self, key: str) -> bytes | str | None: ...
    def set(self, key: str, value: str, ex: int | None = None) -> object: ...
    def delete(self, key: str) -> int: ...
    def keys(self, pattern: str) -> list[bytes | str]: ...
    def ping(self) -> object: ...


class RedisSessionLeaseStore(SessionLeaseStore):
    def __init__(self, redis_url: str, client: RedisLeaseClient | None = None, prefix: str = "voicebot:session_lease") -> None:
        super().__init__()
        self.redis_url = redis_url
        self.prefix = prefix.strip(":") or "voicebot:session_lease"
        self.client = client or _redis_client_from_url(redis_url)
        self.client.ping()

    def acquire(
        self,
        workspace_id: str,
        voicebot_id: str,
        session_id: str,
        owner: str,
        ttl_seconds: float,
        call_id: str | None = None,
        transport: str | None = None,
        metadata: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> SessionLease | None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        current = now or datetime.now(UTC)
        key = self._key(workspace_id, voicebot_id, session_id)
        existing = self._get_key(key, current)
        if existing is not None and existing.owner != owner:
            return None
        lease = SessionLease(
            workspace_id=workspace_id,
            voicebot_id=voicebot_id,
            session_id=session_id,
            owner=owner,
            expires_at=(current + timedelta(seconds=ttl_seconds)).isoformat(),
            call_id=call_id or (existing.call_id if existing is not None else None),
            transport=transport or (existing.transport if existing is not None else None),
            metadata=dict(metadata if metadata is not None else (existing.metadata if existing is not None else {}) or {}),
        )
        self.client.set(key, json.dumps(lease.as_dict(), sort_keys=True), ex=max(1, int(ttl_seconds)))
        return lease

    def renew(
        self,
        workspace_id: str,
        voicebot_id: str,
        session_id: str,
        owner: str,
        ttl_seconds: float,
        call_id: str | None = None,
        transport: str | None = None,
        metadata: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> SessionLease | None:
        existing = self.get(workspace_id, voicebot_id, session_id, now=now)
        if existing is None or existing.owner != owner:
            return None
        return self.acquire(
            workspace_id,
            voicebot_id,
            session_id,
            owner,
            ttl_seconds,
            call_id=call_id,
            transport=transport,
            metadata=metadata,
            now=now,
        )

    def release(self, workspace_id: str, voicebot_id: str, session_id: str, owner: str | None = None) -> SessionLease | None:
        key = self._key(workspace_id, voicebot_id, session_id)
        existing = self._get_key(key)
        if existing is None:
            return None
        if owner is not None and existing.owner != owner:
            return None
        self.client.delete(key)
        return existing

    def get(self, workspace_id: str, voicebot_id: str, session_id: str, now: datetime | None = None) -> SessionLease | None:
        _ = now
        return self._get_key(self._key(workspace_id, voicebot_id, session_id), now)

    def expire(self, now: datetime | None = None) -> tuple[SessionLease, ...]:
        current = now or datetime.now(UTC)
        expired: list[SessionLease] = []
        for key in self.client.keys(f"{self.prefix}:*"):
            key_text = _decode(key)
            lease = self._get_key(key_text)
            if lease is None:
                continue
            if datetime.fromisoformat(lease.expires_at) > current:
                continue
            self.client.delete(key_text)
            expired.append(lease)
        return tuple(expired)

    def list(self, workspace_id: str | None = None, voicebot_id: str | None = None, now: datetime | None = None) -> tuple[SessionLease, ...]:
        self.expire(now)
        leases = []
        for key in self.client.keys(f"{self.prefix}:*"):
            lease = self._get_key(_decode(key))
            if lease is None:
                continue
            if workspace_id is not None and lease.workspace_id != workspace_id:
                continue
            if voicebot_id is not None and lease.voicebot_id != voicebot_id:
                continue
            leases.append(lease)
        return tuple(sorted(leases, key=lambda item: item.lease_key))

    def _key(self, workspace_id: str, voicebot_id: str, session_id: str) -> str:
        return f"{self.prefix}:{lease_key(workspace_id, voicebot_id, session_id)}"

    def _get_key(self, key: str, now: datetime | None = None) -> SessionLease | None:
        payload = self.client.get(key)
        if payload is None:
            return None
        lease = session_lease_from_dict(json.loads(_decode(payload)))
        if now is not None and datetime.fromisoformat(lease.expires_at) <= now:
            self.client.delete(key)
            return None
        return lease


def _redis_client_from_url(redis_url: str) -> RedisLeaseClient:
    try:
        import redis
    except ImportError as exc:
        raise StorageUnavailable(
            "redis package is not installed",
            family="session_leases",
            driver="redis",
        ) from exc
    return redis.Redis.from_url(redis_url)


def _decode(value: bytes | str) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else value
