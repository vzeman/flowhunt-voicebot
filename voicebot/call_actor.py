from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import threading
from typing import Any, Literal
from uuid import uuid4


ActorLane = Literal["audio_input", "stt", "agent", "tts_playback", "control", "background"]
ActorSignalType = Literal["queued", "started", "completed", "cancelled"]

DEFAULT_ACTOR_LANES: tuple[ActorLane, ...] = (
    "audio_input",
    "stt",
    "agent",
    "tts_playback",
    "control",
    "background",
)


@dataclass(frozen=True)
class ActorSignal:
    signal_id: str
    call_id: str
    lane: ActorLane
    type: ActorSignalType
    reason: str = ""
    correlation_id: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    generation: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "call_id": self.call_id,
            "lane": self.lane,
            "type": self.type,
            "reason": self.reason,
            "correlation_id": self.correlation_id,
            "created_at": self.created_at,
            "generation": self.generation,
        }


@dataclass
class ActorLaneState:
    queued: int = 0
    active: int = 0
    cancellation_generation: int = 0
    last_signal: ActorSignal | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "queued": self.queued,
            "active": self.active,
            "cancellation_generation": self.cancellation_generation,
            "last_signal": self.last_signal.as_dict() if self.last_signal else None,
        }


class CallActorCoordinator:
    def __init__(self, call_id: str, lanes: tuple[ActorLane, ...] = DEFAULT_ACTOR_LANES) -> None:
        self._lock = threading.RLock()
        self.call_id = call_id
        self._lanes = {lane: ActorLaneState() for lane in lanes}

    def update_call_id(self, call_id: str) -> None:
        with self._lock:
            self.call_id = call_id

    def queued(self, lane: ActorLane, *, correlation_id: str | None = None, reason: str = "") -> ActorSignal:
        with self._lock:
            state = self._state(lane)
            state.queued += 1
            return self._record_locked(lane, "queued", state, correlation_id=correlation_id, reason=reason)

    def started(self, lane: ActorLane, *, correlation_id: str | None = None, reason: str = "") -> ActorSignal:
        with self._lock:
            state = self._state(lane)
            if state.queued > 0:
                state.queued -= 1
            state.active += 1
            return self._record_locked(lane, "started", state, correlation_id=correlation_id, reason=reason)

    def completed(self, lane: ActorLane, *, correlation_id: str | None = None, reason: str = "") -> ActorSignal:
        with self._lock:
            state = self._state(lane)
            if state.active > 0:
                state.active -= 1
            return self._record_locked(lane, "completed", state, correlation_id=correlation_id, reason=reason)

    def cancel(self, lane: ActorLane, *, reason: str, correlation_id: str | None = None) -> ActorSignal:
        with self._lock:
            state = self._state(lane)
            state.queued = 0
            state.active = 0
            state.cancellation_generation += 1
            return self._record_locked(lane, "cancelled", state, correlation_id=correlation_id, reason=reason)

    def cancel_many(self, lanes: tuple[ActorLane, ...], *, reason: str, correlation_id: str | None = None) -> tuple[ActorSignal, ...]:
        return tuple(self.cancel(lane, reason=reason, correlation_id=correlation_id) for lane in lanes)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "call_id": self.call_id,
                "lanes": {lane: state.as_dict() for lane, state in self._lanes.items()},
            }

    def _state(self, lane: ActorLane) -> ActorLaneState:
        try:
            return self._lanes[lane]
        except KeyError as exc:
            raise ValueError(f"unknown actor lane: {lane}") from exc

    def _record_locked(
        self,
        lane: ActorLane,
        signal_type: ActorSignalType,
        state: ActorLaneState,
        *,
        correlation_id: str | None,
        reason: str,
    ) -> ActorSignal:
        signal = ActorSignal(
            signal_id=str(uuid4()),
            call_id=self.call_id,
            lane=lane,
            type=signal_type,
            reason=reason,
            correlation_id=correlation_id,
            generation=state.cancellation_generation,
        )
        state.last_signal = signal
        return signal
