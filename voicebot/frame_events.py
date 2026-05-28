from __future__ import annotations

from typing import get_args

from .events import EventType
from .frames import Frame, FrameKind


NON_EVENT_FRAME_KINDS: frozenset[FrameKind] = frozenset(
    {
        "audio_input",
        "audio_output",
        "interrupt",
        "cancel_agent",
        "cancel_tts",
        "pause_input",
        "resume_input",
        "flush_playback",
    }
)


FRAME_EVENT_TYPES: dict[str, EventType] = {
    "call_started": "call_started",
    "call_connected": "call_connected",
    "call_ended": "call_ended",
    "dtmf": "dtmf",
    "speech_started": "user_speech_started",
    "speech_finished": "user_speech_finished",
    "transcription_started": "stt_started",
    "transcription_partial": "user_transcript_partial",
    "transcription_finished": "stt_finished",
    "transcription_empty": "stt_no_text",
    "user_transcript": "user_transcript",
    "agent_request": "agent_response_requested",
    "agent_response_partial": "agent_response_partial",
    "agent_response": "agent_response_received",
    "agent_response_dropped": "agent_response_dropped",
    "tts_started": "tts_started",
    "tts_finished": "tts_finished",
    "tts_failed": "tts_failed",
    "playback_started": "bot_playback_started",
    "playback_interrupted": "bot_playback_interrupted",
    "playback_finished": "bot_playback_finished",
    "call_control_requested": "call_control_requested",
    "call_control_completed": "call_control_completed",
    "metrics": "metrics",
    "error": "system",
    "system": "system",
}


def frame_to_event_type(frame: Frame) -> EventType | None:
    return FRAME_EVENT_TYPES.get(frame.kind)


def frame_to_event_data(frame: Frame) -> dict:
    return dict(frame.data)


def frame_event_mapping_issues(mapping: dict[str, str] = FRAME_EVENT_TYPES) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    declared_frames = set(get_args(FrameKind))
    declared_events = set(get_args(EventType))
    seen: set[str] = set()

    for frame_kind, event_type in mapping.items():
        if not frame_kind.strip():
            issues.append({"frame_kind": frame_kind, "event_type": event_type, "issue": "frame kind is required"})
            continue
        if frame_kind in seen:
            issues.append({"frame_kind": frame_kind, "event_type": event_type, "issue": "duplicate frame kind mapping"})
        seen.add(frame_kind)
        if frame_kind not in declared_frames:
            issues.append({"frame_kind": frame_kind, "event_type": event_type, "issue": "frame kind is not declared"})
        if event_type not in declared_events:
            issues.append({"frame_kind": frame_kind, "event_type": event_type, "issue": "event type is not declared"})

    missing = declared_frames - seen - set(NON_EVENT_FRAME_KINDS)
    for frame_kind in sorted(missing):
        issues.append({"frame_kind": frame_kind, "event_type": "", "issue": "persistable frame kind missing event mapping"})
    return issues
