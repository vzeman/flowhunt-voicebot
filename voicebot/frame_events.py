from __future__ import annotations

from .events import EventType
from .frames import Frame


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
