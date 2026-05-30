from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Callable, Protocol


class EventAppender(Protocol):
    def append(self, call_id: str, event_type: str, data: dict | None = None):
        ...


@dataclass
class PendingTurn:
    turn_id: int | None
    transcript_event_id: int | None
    text: str
    created_at: float
    timer: threading.Timer | None = None


class TurnCoalescer:
    def __init__(
        self,
        *,
        call_id: Callable[[], str],
        events: EventAppender,
        emit_request: Callable[[dict], object],
        can_delay_or_merge: Callable[[], bool],
        window_seconds: float,
        max_chars: int,
    ) -> None:
        self._call_id = call_id
        self._events = events
        self._emit_request = emit_request
        self._can_delay_or_merge = can_delay_or_merge
        self._window_seconds = max(0.0, float(window_seconds))
        self._max_chars = max(1, int(max_chars))
        self._lock = threading.RLock()
        self._pending: PendingTurn | None = None

    def handle(self, request_data: dict) -> object | None:
        text = str(request_data.get("text") or "").strip()
        if not self._can_coalesce_text(text) or request_data.get("stale") or not self._can_delay_or_merge():
            self.flush(reason="new_turn_not_delayable")
            return self._emit_request(request_data)

        now = time.monotonic()
        with self._lock:
            pending = self._pending
            if pending is not None and now - pending.created_at <= self._window_seconds and self._can_delay_or_merge():
                self._cancel_timer(pending)
                self._pending = None
                merged_text = coalesce_text(pending.text, text)
                self._events.append(
                    self._call_id(),
                    "turn_coalesced",
                    {
                        "previous_turn_id": pending.turn_id,
                        "current_turn_id": request_data.get("turn_id"),
                        "previous_transcript_event_id": pending.transcript_event_id,
                        "current_transcript_event_id": request_data.get("transcript_event_id"),
                        "text": merged_text,
                        "window_seconds": self._window_seconds,
                    },
                )
                return self._emit_request(
                    {
                        **request_data,
                        "text": merged_text,
                        "coalesced": True,
                        "coalesced_turn_ids": [pending.turn_id, request_data.get("turn_id")],
                        "coalesced_transcript_event_ids": [
                            pending.transcript_event_id,
                            request_data.get("transcript_event_id"),
                        ],
                    }
                )

            self._flush_locked(reason="superseded_by_new_pending")
            pending = PendingTurn(
                turn_id=_optional_int(request_data.get("turn_id")),
                transcript_event_id=_optional_int(request_data.get("transcript_event_id")),
                text=text,
                created_at=now,
            )
            pending.timer = threading.Timer(self._window_seconds, self.flush, kwargs={"reason": "coalesce_window_elapsed"})
            pending.timer.daemon = True
            self._pending = pending
            pending.timer.start()
            return None

    def flush(self, *, reason: str = "manual") -> object | None:
        with self._lock:
            return self._flush_locked(reason=reason)

    def _flush_locked(self, *, reason: str) -> object | None:
        pending = self._pending
        if pending is None:
            return None
        self._cancel_timer(pending)
        self._pending = None
        return self._emit_request(
            {
                "turn_id": pending.turn_id,
                "transcript_event_id": pending.transcript_event_id,
                "text": pending.text,
                "coalesced": False,
                "coalesce_flush_reason": reason,
            }
        )

    def _can_coalesce_text(self, text: str) -> bool:
        return self._window_seconds > 0 and 0 < len(text) <= self._max_chars

    @staticmethod
    def _cancel_timer(pending: PendingTurn) -> None:
        if pending.timer is not None:
            pending.timer.cancel()


def coalesce_text(previous: str, current: str) -> str:
    first = previous.strip()
    second = current.strip()
    if not first:
        return second
    if not second:
        return first
    return f"{first} {second}"


def _optional_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
