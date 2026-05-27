from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import threading
import time
from typing import Any


@dataclass
class AgentTaskTracker:
    responded_event_ids: set[int]

    def __init__(self, max_responded_event_ids: int = 10000) -> None:
        if max_responded_event_ids < 1:
            raise ValueError("max_responded_event_ids must be at least 1")
        self.max_responded_event_ids = max_responded_event_ids
        self.responded_event_ids = set()
        self._responded_order: deque[int] = deque()
        self._responded_event_id_floor = 0
        self._claimed_event_ids: dict[int, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def mark_responded(self, event_id: int | None) -> None:
        if event_id is not None:
            with self._lock:
                if event_id not in self.responded_event_ids:
                    self.responded_event_ids.add(event_id)
                    self._responded_order.append(event_id)
                    self._prune_responded_locked()
                self._claimed_event_ids.pop(event_id, None)

    def is_pending(self, event_id: int, now: float | None = None) -> bool:
        with self._lock:
            self._expire_claims_locked(now or time.monotonic())
            return (
                event_id > self._responded_event_id_floor
                and event_id not in self.responded_event_ids
                and event_id not in self._claimed_event_ids
            )

    def claim(self, event_ids: list[int], owner: str, ttl_seconds: float) -> list[int]:
        now = time.monotonic()
        expires_at = now + max(ttl_seconds, 0.1)
        claimed: list[int] = []
        with self._lock:
            self._expire_claims_locked(now)
            for event_id in event_ids:
                if (
                    event_id <= self._responded_event_id_floor
                    or event_id in self.responded_event_ids
                    or event_id in self._claimed_event_ids
                ):
                    continue
                self._claimed_event_ids[event_id] = (owner, expires_at)
                claimed.append(event_id)
        return claimed

    def release(self, event_id: int | None) -> None:
        if event_id is None:
            return
        with self._lock:
            self._claimed_event_ids.pop(event_id, None)

    def release_many(self, event_ids: list[int], owner: str | None = None) -> list[int]:
        released: list[int] = []
        with self._lock:
            for event_id in event_ids:
                claim = self._claimed_event_ids.get(event_id)
                if claim is not None and (owner is None or claim[0] == owner):
                    self._claimed_event_ids.pop(event_id, None)
                    released.append(event_id)
        return released

    def renew_many(self, event_ids: list[int], owner: str, ttl_seconds: float) -> list[int]:
        now = time.monotonic()
        expires_at = now + max(ttl_seconds, 0.1)
        renewed: list[int] = []
        with self._lock:
            self._expire_claims_locked(now)
            for event_id in event_ids:
                claim = self._claimed_event_ids.get(event_id)
                if claim is not None and claim[0] == owner:
                    self._claimed_event_ids[event_id] = (owner, expires_at)
                    renewed.append(event_id)
        return renewed

    def snapshot(self, owner: str | None = None) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            self._expire_claims_locked(now)
            claims = {
                str(event_id): {
                    "owner": claim_owner,
                    "expires_in_seconds": max(0.0, expires_at - now),
                }
                for event_id, (claim_owner, expires_at) in sorted(self._claimed_event_ids.items())
                if owner is None or claim_owner == owner
            }
            responded = sorted(self.responded_event_ids)
        return {
            "responded_event_ids": responded,
            "responded_event_id_retention": self.max_responded_event_ids,
            "responded_event_id_floor": self._responded_event_id_floor,
            "claims": claims,
        }

    def _expire_claims_locked(self, now: float) -> None:
        expired = [event_id for event_id, (_owner, expires_at) in self._claimed_event_ids.items() if expires_at <= now]
        for event_id in expired:
            self._claimed_event_ids.pop(event_id, None)

    def _prune_responded_locked(self) -> None:
        while len(self._responded_order) > self.max_responded_event_ids:
            event_id = self._responded_order.popleft()
            self.responded_event_ids.discard(event_id)
            self._responded_event_id_floor = max(self._responded_event_id_floor, event_id)
