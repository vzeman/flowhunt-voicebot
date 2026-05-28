from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import uuid4

from .events import VoicebotEvent, event_to_dict, utc_now


TimelineCategory = Literal[
    "call",
    "caller_audio",
    "stt",
    "agent",
    "tts",
    "playback",
    "task",
    "control",
    "transport",
    "telemetry",
    "system",
]


EVENT_CATEGORIES: dict[str, TimelineCategory] = {
    "call_started": "call",
    "call_connected": "call",
    "call_ended": "call",
    "user_speech_started": "caller_audio",
    "user_speech_finished": "caller_audio",
    "stt_started": "stt",
    "stt_finished": "stt",
    "stt_no_text": "stt",
    "user_transcript_partial": "stt",
    "user_transcript": "stt",
    "agent_response_requested": "agent",
    "agent_response_partial": "agent",
    "agent_response_received": "agent",
    "agent_response_deferred": "agent",
    "agent_response_dropped": "agent",
    "agent_response_queued": "agent",
    "tts_started": "tts",
    "tts_finished": "tts",
    "tts_failed": "tts",
    "bot_playback_started": "playback",
    "bot_playback_interrupted": "playback",
    "bot_playback_finished": "playback",
    "agent_task_claimed": "task",
    "agent_task_renewed": "task",
    "agent_task_released": "task",
    "flowhunt_issue_created": "task",
    "flowhunt_issue_updated": "task",
    "flowhunt_issue_completed": "task",
    "flowhunt_flow_invoked": "task",
    "flowhunt_flow_updated": "task",
    "flowhunt_flow_completed": "task",
    "provider_call_failed": "telemetry",
    "subagent_task_requested": "task",
    "subagent_task_deduplicated": "task",
    "subagent_task_updated": "task",
    "subagent_task_completed": "task",
    "subagent_task_failed": "task",
    "subagent_task_timed_out": "task",
    "subagent_task_cancelled": "task",
    "subagent_task_late_completed": "task",
    "call_control_requested": "control",
    "call_control_completed": "control",
    "dtmf": "control",
    "multimodal_content_added": "agent",
    "transport_error": "transport",
    "metrics": "telemetry",
    "context_compacted": "system",
    "system": "system",
}


@dataclass(frozen=True)
class TraceContext:
    trace_id: str = field(default_factory=lambda: str(uuid4()))
    workspace_id: str | None = None
    voicebot_id: str | None = None
    session_id: str | None = None
    call_id: str | None = None
    turn_id: int | None = None
    event_id: int | None = None

    @classmethod
    def from_event(cls, event: VoicebotEvent) -> "TraceContext":
        data = event.data
        return cls(
            trace_id=str(data.get("trace_id") or data.get("trace") or uuid4()),
            workspace_id=_optional_str(data.get("workspace_id")),
            voicebot_id=_optional_str(data.get("voicebot_id")),
            session_id=_optional_str(data.get("session_id", event.call_id)),
            call_id=event.call_id,
            turn_id=_optional_int(data.get("turn_id")),
            event_id=event.id,
        )

    def to_log_fields(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "workspace_id": self.workspace_id,
            "voicebot_id": self.voicebot_id,
            "session_id": self.session_id,
            "call_id": self.call_id,
            "turn_id": self.turn_id,
            "event_id": self.event_id,
        }


def structured_log_record(
    level: str,
    message: str,
    context: TraceContext,
    **fields: Any,
) -> dict[str, Any]:
    return {
        "timestamp": utc_now(),
        "level": level.lower(),
        "message": message,
        **context.to_log_fields(),
        **fields,
    }


def build_timeline(events: list[VoicebotEvent]) -> dict[str, Any]:
    entries = []
    category_counts: dict[str, int] = {}
    for event in sorted(events, key=lambda item: item.id):
        category = EVENT_CATEGORIES.get(event.type, "system")
        category_counts[category] = category_counts.get(category, 0) + 1
        entries.append(
            {
                "id": event.id,
                "timestamp": event.timestamp,
                "call_id": event.call_id,
                "type": event.type,
                "category": category,
                "trace": TraceContext.from_event(event).to_log_fields(),
                "data": event.data,
            }
        )
    return {
        "events": entries,
        "counts": category_counts,
        "audio": audio_observability_summary(events),
        "providers": provider_observability_summary(events)["providers"],
        "first_event_id": entries[0]["id"] if entries else None,
        "last_event_id": entries[-1]["id"] if entries else None,
    }


def audio_observability_summary(events: list[VoicebotEvent]) -> dict[str, Any]:
    speech_started = _count_events(events, "user_speech_started")
    speech_finished = _count_events(events, "user_speech_finished")
    playback_started = _count_events(events, "bot_playback_started")
    playback_finished = _count_events(events, "bot_playback_finished")
    playback_interrupted = _count_events(events, "bot_playback_interrupted")
    return {
        "speech_turns_started": speech_started,
        "speech_turns_finished": speech_finished,
        "stt_no_text": _count_events(events, "stt_no_text"),
        "transcripts": _count_events(events, "user_transcript"),
        "partial_transcripts": _count_events(events, "user_transcript_partial"),
        "playback_started": playback_started,
        "playback_finished": playback_finished,
        "playback_interrupted": playback_interrupted,
        "possible_barge_ins": playback_interrupted,
        "open_speech_turns": max(0, speech_started - speech_finished),
        "open_playbacks": max(0, playback_started - playback_finished - playback_interrupted),
    }


def provider_observability_summary(events: list[VoicebotEvent]) -> dict[str, Any]:
    latencies: dict[str, list[float]] = {}
    failures: dict[str, int] = {}
    for event in events:
        provider = event.data.get("provider")
        if not provider:
            provider = _provider_from_event_type(event.type)
        if not provider:
            continue
        provider = str(provider)
        if event.type == "metrics" and event.data.get("name"):
            value = _optional_float(event.data.get("value"))
            if value is not None:
                latencies.setdefault(provider, []).append(value)
        if event.type.endswith("_failed") or event.type in {"transport_error", "subagent_task_failed", "provider_call_failed"}:
            failures[provider] = failures.get(provider, 0) + 1
    return {
        "providers": {
            provider: {
                "latency_count": len(values),
                "latency_avg": sum(values) / len(values) if values else None,
                "failure_count": failures.get(provider, 0),
            }
            for provider, values in sorted(latencies.items())
        }
        | {
            provider: {
                "latency_count": 0,
                "latency_avg": None,
                "failure_count": count,
            }
            for provider, count in sorted(failures.items())
            if provider not in latencies
        }
    }


@dataclass(frozen=True)
class ConversationExpectation:
    must_include_event_types: tuple[str, ...] = ()
    max_duplicate_agent_responses: int = 1
    require_final_agent_response: bool = False


def evaluate_conversation(events: list[VoicebotEvent], expectation: ConversationExpectation) -> dict[str, Any]:
    failures: list[str] = []
    event_types = [event.type for event in events]
    for event_type in expectation.must_include_event_types:
        if event_type not in event_types:
            failures.append(f"missing event type: {event_type}")

    responses = [
        str(event.data.get("text", "")).strip()
        for event in events
        if event.type in {"agent_response_received", "agent_response_queued"} and str(event.data.get("text", "")).strip()
    ]
    duplicate_count = _max_consecutive_duplicates(responses)
    if duplicate_count > expectation.max_duplicate_agent_responses:
        failures.append(f"duplicate agent response repeated {duplicate_count} times")
    if expectation.require_final_agent_response and "agent_response_received" not in event_types:
        failures.append("missing final agent response")

    return {
        "ok": not failures,
        "failures": failures,
        "event_count": len(events),
        "timeline": build_timeline(events),
    }


def _provider_from_event_type(event_type: str) -> str | None:
    if event_type.startswith("stt_"):
        return "stt"
    if event_type.startswith("tts_"):
        return "tts"
    if event_type.startswith("flowhunt_") or event_type.startswith("subagent_"):
        return "subagent"
    if event_type.startswith("agent_"):
        return "agent"
    return None


def _max_consecutive_duplicates(values: list[str]) -> int:
    max_count = 0
    current_value = None
    current_count = 0
    for value in values:
        if value == current_value:
            current_count += 1
        else:
            current_value = value
            current_count = 1
        max_count = max(max_count, current_count)
    return max_count


def _count_events(events: list[VoicebotEvent], event_type: str) -> int:
    return sum(1 for event in events if event.type == event_type)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
