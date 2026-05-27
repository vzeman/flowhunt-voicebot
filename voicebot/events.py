from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import count
from typing import Any, Literal
import threading

from .transcripts import TranscriptStore


EventType = Literal[
    "call_started",
    "call_connected",
    "call_ended",
    "call_control_requested",
    "call_control_completed",
    "user_speech_started",
    "user_speech_finished",
    "stt_started",
    "stt_finished",
    "stt_no_text",
    "user_transcript_partial",
    "user_transcript",
    "agent_response_requested",
    "agent_response_partial",
    "agent_response_received",
    "agent_response_dropped",
    "agent_response_queued",
    "tts_started",
    "tts_finished",
    "tts_failed",
    "bot_playback_started",
    "bot_playback_interrupted",
    "bot_playback_finished",
    "dtmf",
    "system",
    "context_compacted",
]


_event_ids = count(1)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class VoicebotEvent:
    id: int
    call_id: str
    type: EventType
    timestamp: str
    data: dict[str, Any] = field(default_factory=dict)


class EventStore:
    def __init__(self, max_context_events: int, transcript_store: TranscriptStore | None = None) -> None:
        self._lock = threading.Lock()
        self._events: list[VoicebotEvent] = []
        self._summary = ""
        self._max_context_events = max_context_events
        self._transcript_store = transcript_store

    def append(self, call_id: str, event_type: EventType, data: dict[str, Any] | None = None) -> VoicebotEvent:
        event = VoicebotEvent(
            id=next(_event_ids),
            call_id=call_id,
            type=event_type,
            timestamp=utc_now(),
            data=data or {},
        )
        with self._lock:
            self._events.append(event)
            if self._transcript_store is not None:
                self._transcript_store.append(event)
            self._compact_locked()
        return event

    def list_events(self, after: int = 0, call_id: str | None = None, limit: int = 200) -> list[VoicebotEvent]:
        with self._lock:
            events = [e for e in self._events if e.id > after and (call_id is None or e.call_id == call_id)]
            return events[:limit]

    def context(self, call_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            events = [e for e in self._events if call_id is None or e.call_id == call_id]
            return {
                "summary": self._summary,
                "events": [event_to_dict(e) for e in events],
            }

    def replace_summary(self, summary: str, call_id: str = "system") -> VoicebotEvent:
        with self._lock:
            self._summary = summary.strip()
        return self.append(call_id, "context_compacted", {"summary": self._summary})

    def _compact_locked(self) -> None:
        if len(self._events) <= self._max_context_events:
            return

        overflow = self._events[: len(self._events) - self._max_context_events]
        self._events = self._events[-self._max_context_events :]
        lines = []
        if self._summary:
            lines.append(self._summary)
        for event in overflow:
            if event.type in {
                "call_started",
                "call_connected",
                "call_ended",
                "user_transcript",
                "agent_response_requested",
                "agent_response_received",
                "call_control_completed",
            }:
                lines.append(f"{event.timestamp} {event.call_id} {event.type}: {event.data}")
        self._summary = "\n".join(lines)[-6000:]


def event_to_dict(event: VoicebotEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "call_id": event.call_id,
        "type": event.type,
        "timestamp": event.timestamp,
        "data": event.data,
    }
