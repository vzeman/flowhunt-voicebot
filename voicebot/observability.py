from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
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
    audio = audio_observability_summary(events)
    providers = provider_observability_summary(events)["providers"]
    return {
        "events": entries,
        "counts": category_counts,
        "audio": audio,
        "providers": providers,
        "latency": latency_observability_summary(events),
        "health": timeline_health_summary(audio, providers),
        "first_event_id": entries[0]["id"] if entries else None,
        "last_event_id": entries[-1]["id"] if entries else None,
        "duration_seconds": timeline_duration_seconds(events),
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


def latency_observability_summary(events: list[VoicebotEvent]) -> dict[str, Any]:
    ordered = sorted(events, key=lambda item: item.id)
    turns = _turn_latency_breakdowns(ordered)
    metric_summary = _metric_latency_summary(ordered)
    complete_turns = [
        turn for turn in turns if turn.get("end_of_speech_to_playback_started_seconds") is not None
    ]
    slowest = max(
        complete_turns,
        key=lambda turn: float(turn["end_of_speech_to_playback_started_seconds"]),
        default=None,
    )
    return {
        "turns": turns,
        "metrics": metric_summary,
        "slowest_turn": slowest,
    }


def timeline_health_summary(audio: dict[str, Any], providers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    warnings: list[str] = []
    if int(audio.get("open_speech_turns") or 0) > 0:
        warnings.append("open speech turn")
    if int(audio.get("open_playbacks") or 0) > 0:
        warnings.append("open playback")
    failed_providers = sorted(
        provider
        for provider, summary in providers.items()
        if int(summary.get("failure_count") or 0) > 0
    )
    if failed_providers:
        warnings.append(f"provider failures: {', '.join(failed_providers)}")
    return {
        "ok": not warnings,
        "warnings": warnings,
        "failed_providers": failed_providers,
    }


def timeline_duration_seconds(events: list[VoicebotEvent]) -> float | None:
    if len(events) < 2:
        return None
    ordered = sorted(events, key=lambda item: item.id)
    try:
        started = _parse_timestamp(ordered[0].timestamp)
        ended = _parse_timestamp(ordered[-1].timestamp)
    except ValueError:
        return None
    return max(0.0, (ended - started).total_seconds())


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


def _turn_latency_breakdowns(events: list[VoicebotEvent]) -> list[dict[str, Any]]:
    turn_ids = sorted(
        {
            turn_id
            for event in events
            for turn_id in [_optional_int(event.data.get("turn_id"))]
            if turn_id is not None
        }
    )
    turns = []
    for turn_id in turn_ids:
        speech_started = _first_event(events, "user_speech_started", turn_id=turn_id)
        speech_finished = _first_event(events, "user_speech_finished", turn_id=turn_id)
        stt_started = _first_event(events, "stt_started", turn_id=turn_id)
        stt_finished = _first_event(events, "stt_finished", turn_id=turn_id)
        transcript = _first_event(events, "user_transcript", turn_id=turn_id)
        request = _first_event(events, "agent_response_requested", turn_id=turn_id)
        response = _first_response_for_request(events, request.id if request else None)
        tts_started = _first_response_event(events, "tts_started", request.id if request else None)
        queued = _first_response_event(events, "agent_response_queued", request.id if request else None)
        playback_started = _first_response_event(events, "bot_playback_started", request.id if request else None)
        if playback_started is None:
            playback_started = _first_event_after(events, "bot_playback_started", queued)
        turns.append(
            {
                "turn_id": turn_id,
                "event_ids": {
                    "speech_started": speech_started.id if speech_started else None,
                    "speech_finished": speech_finished.id if speech_finished else None,
                    "stt_started": stt_started.id if stt_started else None,
                    "stt_finished": stt_finished.id if stt_finished else None,
                    "transcript": transcript.id if transcript else None,
                    "agent_request": request.id if request else None,
                    "agent_response": response.id if response else None,
                    "tts_started": tts_started.id if tts_started else None,
                    "agent_response_queued": queued.id if queued else None,
                    "playback_started": playback_started.id if playback_started else None,
                },
                "speech_to_transcript_seconds": _seconds_between_events(speech_finished, transcript),
                "transcript_to_agent_response_seconds": _seconds_between_events(transcript, response),
                "agent_response_to_tts_started_seconds": _seconds_between_events(response, tts_started),
                "agent_response_to_playback_started_seconds": _seconds_between_events(response, playback_started),
                "end_of_speech_to_playback_started_seconds": _seconds_between_events(speech_finished, playback_started),
            }
        )
    return turns


def _metric_latency_summary(events: list[VoicebotEvent]) -> dict[str, Any]:
    values_by_name: dict[str, list[tuple[VoicebotEvent, float]]] = {}
    for event in events:
        if event.type != "metrics":
            continue
        name = str(event.data.get("name") or "")
        value = _optional_float(event.data.get("value"))
        if not name or value is None:
            continue
        values_by_name.setdefault(name, []).append((event, value))
    return {
        name: {
            "count": len(values),
            "avg": sum(value for _event, value in values) / len(values),
            "max": max(value for _event, value in values),
            "latest": {
                "event_id": values[-1][0].id,
                "value": values[-1][1],
                "timestamp": values[-1][0].timestamp,
            },
        }
        for name, values in sorted(values_by_name.items())
    }


def _first_event(events: list[VoicebotEvent], event_type: str, turn_id: int | None = None) -> VoicebotEvent | None:
    for event in events:
        if event.type != event_type:
            continue
        if turn_id is not None and _optional_int(event.data.get("turn_id")) != turn_id:
            continue
        return event
    return None


def _first_response_for_request(events: list[VoicebotEvent], request_event_id: int | None) -> VoicebotEvent | None:
    return _first_response_event(events, "agent_response_received", request_event_id)


def _first_response_event(
    events: list[VoicebotEvent],
    event_type: str,
    request_event_id: int | None,
) -> VoicebotEvent | None:
    if request_event_id is None:
        return None
    for event in events:
        if event.type == event_type and _optional_int(event.data.get("response_to_event_id")) == request_event_id:
            return event
    return None


def _first_event_after(
    events: list[VoicebotEvent],
    event_type: str,
    after_event: VoicebotEvent | None,
) -> VoicebotEvent | None:
    if after_event is None:
        return None
    for event in events:
        if event.id > after_event.id and event.type == event_type:
            return event
    return None


def _seconds_between_events(start: VoicebotEvent | None, end: VoicebotEvent | None) -> float | None:
    if start is None or end is None:
        return None
    try:
        started = _parse_timestamp(start.timestamp)
        ended = _parse_timestamp(end.timestamp)
    except ValueError:
        return None
    return max(0.0, (ended - started).total_seconds())


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


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
