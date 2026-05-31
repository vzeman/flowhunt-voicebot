from __future__ import annotations

from dataclasses import dataclass
import threading
import time


@dataclass(frozen=True)
class PublicAdmissionDecision:
    allowed: bool
    reason: str
    retry_after_seconds: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            **({"retry_after_seconds": self.retry_after_seconds} if self.retry_after_seconds is not None else {}),
        }


class FixedWindowPublicRateLimiter:
    def __init__(self, limit_per_minute: int) -> None:
        self.limit_per_minute = max(1, int(limit_per_minute))
        self._lock = threading.Lock()
        self._counts: dict[tuple[str, int], int] = {}

    def check_and_increment(self, key: str, now: float | None = None) -> PublicAdmissionDecision:
        current = time.time() if now is None else now
        window = int(current // 60)
        counter_key = (key, window)
        with self._lock:
            self._cleanup(window)
            count = self._counts.get(counter_key, 0)
            if count >= self.limit_per_minute:
                retry_after = max(1, int(((window + 1) * 60) - current))
                return PublicAdmissionDecision(False, "public_route_rate_limited", retry_after)
            self._counts[counter_key] = count + 1
        return PublicAdmissionDecision(True, "accepted")

    def _cleanup(self, current_window: int) -> None:
        for key in list(self._counts):
            if key[1] < current_window:
                self._counts.pop(key, None)


def origin_allowed(origin: str | None, allowed_origins: tuple[str, ...]) -> bool:
    if not allowed_origins:
        return True
    if not origin:
        return False
    normalized = normalize_origin(origin)
    return any(normalize_origin(candidate) == normalized for candidate in allowed_origins)


def normalize_origin(value: str) -> str:
    return value.strip().rstrip("/")
