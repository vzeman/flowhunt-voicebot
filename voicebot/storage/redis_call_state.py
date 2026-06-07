from __future__ import annotations

from copy import deepcopy
from typing import Any, Protocol
import json

from ..call_state import CallStateStore, utc_now_iso
from .errors import StorageUnavailable


class RedisCallStateClient(Protocol):
    def get(self, key: str) -> bytes | str | None: ...
    def set(self, key: str, value: str, ex: int | None = None) -> object: ...
    def delete(self, *keys: str) -> int: ...
    def keys(self, pattern: str) -> list[bytes | str]: ...
    def ping(self) -> object: ...


class RedisCallStateStore(CallStateStore):
    def __init__(
        self,
        redis_url: str,
        client: RedisCallStateClient | None = None,
        prefix: str = "voicebot:call_state",
    ) -> None:
        super().__init__()
        self.redis_url = redis_url
        self.prefix = prefix.strip(":") or "voicebot:call_state"
        self.client = client or _redis_client_from_url(redis_url)
        self.client.ping()

    def upsert(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        call_id = _required_str(snapshot, "call_id")
        state = {
            **deepcopy(snapshot),
            "call_id": call_id,
            "state": "active",
            "updated_at": utc_now_iso(),
        }
        self.client.set(self._key(call_id), json.dumps(state, sort_keys=True))
        return deepcopy(state)

    def end(self, call_id: str) -> dict[str, Any] | None:
        normalized = call_id.strip()
        if not normalized:
            raise ValueError("call_id is required")
        existing = self.get(normalized)
        if existing is None:
            return None
        ended = {
            **deepcopy(existing),
            "state": "ended",
            "ended_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
        }
        self.client.set(self._key(normalized), json.dumps(ended, sort_keys=True))
        return deepcopy(ended)

    def get(self, call_id: str) -> dict[str, Any] | None:
        payload = self.client.get(self._key(call_id))
        if payload is None:
            return None
        state = json.loads(_decode(payload))
        return deepcopy(state)

    def list(self, active_only: bool = False) -> tuple[dict[str, Any], ...]:
        states = []
        for key in self.client.keys(f"{self.prefix}:*"):
            payload = self.client.get(_decode(key))
            if payload is None:
                continue
            state = json.loads(_decode(payload))
            if active_only and state.get("state") != "active":
                continue
            states.append(deepcopy(state))
        return tuple(sorted(states, key=lambda item: str(item["call_id"])))

    def _key(self, call_id: str) -> str:
        return f"{self.prefix}:{call_id}"


def _redis_client_from_url(redis_url: str) -> RedisCallStateClient:
    try:
        import redis
    except ImportError as exc:
        raise StorageUnavailable(
            "redis package is not installed",
            family="call_states",
            driver="redis",
        ) from exc
    return redis.Redis.from_url(redis_url)


def _required_str(data: dict[str, Any], field: str) -> str:
    value = data.get(field)
    if value is None:
        raise ValueError(f"{field} is required")
    text = str(value).strip()
    if not text:
        raise ValueError(f"{field} is required")
    return text


def _decode(value: bytes | str) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else value
