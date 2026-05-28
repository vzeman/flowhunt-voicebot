from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import count
from pathlib import Path
from typing import Any, Literal
import json
import threading

from .execution_model import ExecutionIds, ExecutionScope
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
    "agent_response_deferred",
    "agent_response_dropped",
    "agent_response_queued",
    "agent_task_claimed",
    "agent_task_renewed",
    "agent_task_released",
    "flowhunt_issue_created",
    "flowhunt_issue_updated",
    "flowhunt_issue_completed",
    "flowhunt_flow_invoked",
    "flowhunt_flow_updated",
    "flowhunt_flow_completed",
    "provider_call_failed",
    "subagent_task_requested",
    "subagent_task_deduplicated",
    "subagent_task_updated",
    "subagent_task_completed",
    "subagent_task_failed",
    "subagent_task_timed_out",
    "subagent_task_cancelled",
    "subagent_task_late_completed",
    "tts_started",
    "tts_finished",
    "tts_failed",
    "bot_playback_started",
    "bot_playback_interrupted",
    "bot_playback_finished",
    "metrics",
    "dtmf",
    "multimodal_content_added",
    "transport_error",
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
    def __init__(
        self,
        max_context_events: int,
        transcript_store: TranscriptStore | None = None,
        initial_events: list[VoicebotEvent] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._events: list[VoicebotEvent] = list(initial_events or [])
        self._summary = ""
        self._max_context_events = max_context_events
        self._transcript_store = transcript_store
        next_id = max((event.id for event in self._events), default=0) + 1
        self._event_ids = count(next_id)
        self._compact_locked()

    def append(self, call_id: str, event_type: EventType, data: dict[str, Any] | None = None) -> VoicebotEvent:
        event = VoicebotEvent(
            id=next(self._event_ids),
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

    def append_scoped(
        self,
        scope: ExecutionScope,
        event_type: EventType,
        data: dict[str, Any] | None = None,
        ids: ExecutionIds | None = None,
    ) -> VoicebotEvent:
        payload = {
            **(data or {}),
            **scope.to_data(),
            **((ids or ExecutionIds()).to_data()),
        }
        return self.append(scope.call_id or scope.session_id, event_type, payload)

    def list_events(
        self,
        after: int = 0,
        call_id: str | None = None,
        limit: int = 200,
        workspace_id: str | None = None,
        voicebot_id: str | None = None,
        session_id: str | None = None,
    ) -> list[VoicebotEvent]:
        with self._lock:
            events = [
                e
                for e in self._events
                if e.id > after
                and (call_id is None or e.call_id == call_id)
                and _event_matches_scope(e, workspace_id, voicebot_id, session_id)
            ]
            return events[:limit]

    def get_event(self, event_id: int) -> VoicebotEvent | None:
        with self._lock:
            for event in self._events:
                if event.id == event_id:
                    return event
        return None

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
                "agent_task_claimed",
                "agent_task_renewed",
                "agent_task_released",
                "agent_response_received",
                "agent_response_deferred",
                "agent_response_dropped",
                "agent_response_queued",
                "call_control_completed",
                "flowhunt_issue_created",
                "flowhunt_issue_updated",
                "flowhunt_issue_completed",
                "flowhunt_flow_invoked",
                "flowhunt_flow_updated",
                "flowhunt_flow_completed",
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


class JsonEventStore(EventStore):
    def __init__(
        self,
        path: str | Path,
        max_context_events: int,
        transcript_store: TranscriptStore | None = None,
    ) -> None:
        self.path = Path(path)
        self.load_diagnostics: dict[str, int] = {
            "loaded_events": 0,
            "skipped_blank_lines": 0,
            "skipped_malformed_json": 0,
            "skipped_invalid_events": 0,
            "skipped_duplicate_event_ids": 0,
        }
        super().__init__(
            max_context_events=max_context_events,
            transcript_store=transcript_store,
            initial_events=self._load_events(),
        )

    def append(self, call_id: str, event_type: EventType, data: dict[str, Any] | None = None) -> VoicebotEvent:
        event = super().append(call_id, event_type, data)
        self._append_to_log(event)
        return event

    def _load_events(self) -> list[VoicebotEvent]:
        if not self.path.exists():
            return []
        events: list[VoicebotEvent] = []
        diagnostics = dict(self.load_diagnostics)
        seen_ids: set[int] = set()
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    diagnostics["skipped_blank_lines"] += 1
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    diagnostics["skipped_malformed_json"] += 1
                    continue
                if isinstance(payload, dict):
                    event = event_from_dict(payload)
                    if event is not None:
                        if event.id in seen_ids:
                            diagnostics["skipped_duplicate_event_ids"] += 1
                            continue
                        seen_ids.add(event.id)
                        events.append(event)
                        diagnostics["loaded_events"] += 1
                        continue
                diagnostics["skipped_invalid_events"] += 1
        self.load_diagnostics = diagnostics
        return events

    def _append_to_log(self, event: VoicebotEvent) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event_to_dict(event), ensure_ascii=False, sort_keys=True) + "\n")


def event_from_dict(data: dict[str, Any]) -> VoicebotEvent | None:
    try:
        event_id = int(data["id"])
        call_id = str(data["call_id"]).strip()
        event_type = str(data["type"]).strip()
        timestamp = str(data["timestamp"]).strip()
    except (KeyError, TypeError, ValueError):
        return None
    if event_id < 1 or not call_id or not event_type or not timestamp:
        return None
    payload = data.get("data") if isinstance(data.get("data"), dict) else {}
    return VoicebotEvent(event_id, call_id, event_type, timestamp, payload)


def _event_matches_scope(
    event: VoicebotEvent,
    workspace_id: str | None,
    voicebot_id: str | None,
    session_id: str | None,
) -> bool:
    if workspace_id is not None and event.data.get("workspace_id") != workspace_id:
        return False
    if voicebot_id is not None and event.data.get("voicebot_id") != voicebot_id:
        return False
    if session_id is not None and event.data.get("session_id", event.call_id) != session_id:
        return False
    return True
