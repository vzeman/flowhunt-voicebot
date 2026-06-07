from __future__ import annotations

from typing import Protocol
import time

from ..agent_tasks import AgentTaskTracker
from .errors import StorageUnavailable


class RedisAgentTaskClient(Protocol):
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


class RedisAgentTaskTracker(AgentTaskTracker):
    def __init__(
        self,
        redis_url: str,
        max_responded_event_ids: int = 10000,
        client: RedisAgentTaskClient | None = None,
        prefix: str = "voicebot:agent_tasks",
    ) -> None:
        super().__init__(max_responded_event_ids=max_responded_event_ids)
        self.redis_url = redis_url
        self.prefix = prefix.strip(":") or "voicebot:agent_tasks"
        self.client = client or _redis_client_from_url(redis_url)
        self.client.ping()

    def mark_responded(self, event_id: int | None) -> None:
        if event_id is None:
            return
        self.client.set(self._responded_key(event_id), "1")
        self.client.delete(self._claim_key(event_id))
        self._prune_responded()

    def is_pending(self, event_id: int, now: float | None = None) -> bool:
        _ = now
        return (
            event_id > self._responded_floor()
            and self.client.get(self._responded_key(event_id)) is None
            and self.client.get(self._claim_key(event_id)) is None
        )

    def claim(self, event_ids: list[int], owner: str, ttl_seconds: float) -> list[int]:
        ttl_ms = max(1, int(ttl_seconds * 1000))
        claimed: list[int] = []
        floor = self._responded_floor()
        for event_id in event_ids:
            if event_id <= floor or self.client.get(self._responded_key(event_id)) is not None:
                continue
            if self.client.set(self._claim_key(event_id), owner, px=ttl_ms, nx=True):
                claimed.append(event_id)
        return claimed

    def release(self, event_id: int | None) -> None:
        if event_id is not None:
            self.client.delete(self._claim_key(event_id))

    def release_many(self, event_ids: list[int], owner: str | None = None) -> list[int]:
        released: list[int] = []
        for event_id in event_ids:
            key = self._claim_key(event_id)
            claim_owner = self.client.get(key)
            if claim_owner is not None and (owner is None or _decode(claim_owner) == owner):
                self.client.delete(key)
                released.append(event_id)
        return released

    def renew_many(self, event_ids: list[int], owner: str, ttl_seconds: float) -> list[int]:
        ttl_ms = max(1, int(ttl_seconds * 1000))
        renewed: list[int] = []
        for event_id in event_ids:
            key = self._claim_key(event_id)
            if self.client.get(key) is None:
                continue
            if _decode(self.client.get(key)) == owner:
                self.client.set(key, owner, px=ttl_ms)
                renewed.append(event_id)
        return renewed

    def snapshot(self, owner: str | None = None) -> dict:
        claims = {}
        for key in self.client.keys(f"{self.prefix}:claim:*"):
            key_text = _decode(key)
            event_id = _event_id_from_key(key_text)
            claim_owner = self.client.get(key_text)
            if event_id is None or claim_owner is None:
                continue
            claim_owner_text = _decode(claim_owner)
            if owner is not None and claim_owner_text != owner:
                continue
            claims[str(event_id)] = {
                "owner": claim_owner_text,
                "expires_in_seconds": self._ttl_seconds(key_text),
            }
        responded = sorted(self._responded_event_ids())
        self.responded_event_ids = set(responded)
        return {
            "responded_event_ids": responded,
            "responded_event_id_retention": self.max_responded_event_ids,
            "responded_event_id_floor": self._responded_floor(),
            "claims": {key: claims[key] for key in sorted(claims, key=int)},
        }

    def task_state(self, event_id: int, active: bool = True, now: float | None = None) -> dict:
        _ = now
        claim_owner = self.client.get(self._claim_key(event_id))
        if claim_owner is not None:
            return {
                "state": "claimed",
                "owner": _decode(claim_owner),
                "expires_in_seconds": self._ttl_seconds(self._claim_key(event_id)),
            }
        if event_id <= self._responded_floor() or self.client.get(self._responded_key(event_id)) is not None:
            return {"state": "responded"}
        if active:
            return {"state": "pending"}
        return {"state": "inactive"}

    def _responded_event_ids(self) -> list[int]:
        event_ids = []
        for key in self.client.keys(f"{self.prefix}:responded:*"):
            event_id = _event_id_from_key(_decode(key))
            if event_id is not None:
                event_ids.append(event_id)
        return event_ids

    def _prune_responded(self) -> None:
        event_ids = sorted(self._responded_event_ids())
        overflow = len(event_ids) - self.max_responded_event_ids
        if overflow <= 0:
            return
        pruned = event_ids[:overflow]
        self.client.delete(*(self._responded_key(event_id) for event_id in pruned))
        self.client.set(self._floor_key(), str(max([self._responded_floor(), *pruned])))

    def _responded_floor(self) -> int:
        value = self.client.get(self._floor_key())
        try:
            return int(_decode(value)) if value is not None else 0
        except ValueError:
            return 0

    def _ttl_seconds(self, key: str) -> float:
        ttl = getattr(self.client, "ttl", None)
        if ttl is None:
            return 0.0
        try:
            return max(0.0, float(ttl(key)))
        except (TypeError, ValueError):
            return 0.0

    def _claim_key(self, event_id: int) -> str:
        return f"{self.prefix}:claim:{event_id}"

    def _responded_key(self, event_id: int) -> str:
        return f"{self.prefix}:responded:{event_id}"

    def _floor_key(self) -> str:
        return f"{self.prefix}:responded_floor"


def _redis_client_from_url(redis_url: str) -> RedisAgentTaskClient:
    try:
        import redis
    except ImportError as exc:
        raise StorageUnavailable(
            "redis package is not installed",
            family="agent_tasks",
            driver="redis",
        ) from exc
    return redis.Redis.from_url(redis_url)


def _decode(value: bytes | str | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8") if isinstance(value, bytes) else value


def _event_id_from_key(key: str) -> int | None:
    try:
        return int(key.rsplit(":", 1)[-1])
    except ValueError:
        return None
