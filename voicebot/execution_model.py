from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .frames import Frame, FrameKind


FrameCategory = Literal[
    "audio",
    "call_lifecycle",
    "speech_lifecycle",
    "transcription",
    "agent",
    "tts",
    "playback",
    "call_control",
    "control",
    "metrics",
    "system",
]


@dataclass(frozen=True)
class ExecutionScope:
    workspace_id: str = ""
    voicebot_id: str = ""
    session_id: str = ""
    call_id: str = ""

    def to_data(self) -> dict[str, str]:
        return {
            key: value
            for key, value in {
                "workspace_id": self.workspace_id,
                "voicebot_id": self.voicebot_id,
                "session_id": self.session_id,
                "call_id": self.call_id,
            }.items()
            if value
        }

    def require_workspace(self) -> "ExecutionScope":
        if not self.workspace_id:
            raise ValueError("workspace_id is required")
        if not self.voicebot_id:
            raise ValueError("voicebot_id is required")
        if not self.session_id:
            raise ValueError("session_id is required")
        return self

    def same_session(self, other: "ExecutionScope") -> bool:
        return (
            bool(self.workspace_id)
            and bool(self.voicebot_id)
            and bool(self.session_id)
            and self.workspace_id == other.workspace_id
            and self.voicebot_id == other.voicebot_id
            and self.session_id == other.session_id
        )


@dataclass(frozen=True)
class ExecutionIds:
    frame_id: str = ""
    event_id: int | None = None
    turn_id: int | None = None
    request_event_id: int | None = None
    response_to_event_id: int | None = None
    request_frame_id: str = ""
    response_to_frame_id: str = ""
    external_task_id: str = ""
    trace_id: str = ""

    def to_data(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "frame_id": self.frame_id,
                "event_id": self.event_id,
                "turn_id": self.turn_id,
                "request_event_id": self.request_event_id,
                "response_to_event_id": self.response_to_event_id,
                "request_frame_id": self.request_frame_id,
                "response_to_frame_id": self.response_to_frame_id,
                "external_task_id": self.external_task_id,
                "trace_id": self.trace_id,
            }.items()
            if value is not None and value != ""
        }


@dataclass(frozen=True, order=True)
class FrameOrderingKey:
    session_id: str
    turn_id: int
    timestamp: str
    frame_id: str

    @classmethod
    def from_frame(cls, frame: Frame) -> "FrameOrderingKey":
        scope = scope_from_frame(frame)
        ids = ids_from_frame(frame)
        return cls(
            session_id=scope.session_id,
            turn_id=ids.turn_id or 0,
            timestamp=frame.timestamp,
            frame_id=frame.frame_id,
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "timestamp": self.timestamp,
            "frame_id": self.frame_id,
        }


FRAME_CATEGORIES: dict[FrameKind, FrameCategory] = {
    "audio_input": "audio",
    "audio_output": "audio",
    "call_started": "call_lifecycle",
    "call_connected": "call_lifecycle",
    "call_ended": "call_lifecycle",
    "dtmf": "call_control",
    "speech_started": "speech_lifecycle",
    "speech_finished": "speech_lifecycle",
    "transcription_started": "transcription",
    "transcription_partial": "transcription",
    "transcription_finished": "transcription",
    "transcription_empty": "transcription",
    "user_transcript": "transcription",
    "agent_request": "agent",
    "agent_response_partial": "agent",
    "agent_response": "agent",
    "agent_response_dropped": "agent",
    "tts_started": "tts",
    "tts_finished": "tts",
    "tts_failed": "tts",
    "playback_started": "playback",
    "playback_interrupted": "playback",
    "playback_finished": "playback",
    "call_control_requested": "call_control",
    "call_control_completed": "call_control",
    "interrupt": "control",
    "cancel_agent": "control",
    "cancel_tts": "control",
    "pause_input": "control",
    "resume_input": "control",
    "flush_playback": "control",
    "metrics": "metrics",
    "error": "system",
    "system": "system",
}


SESSION_ORDERED_CATEGORIES: set[FrameCategory] = {
    "call_lifecycle",
    "speech_lifecycle",
    "transcription",
    "agent",
    "tts",
    "playback",
    "call_control",
    "control",
}


CANCELLATION_FRAME_KINDS: set[FrameKind] = {
    "interrupt",
    "cancel_agent",
    "cancel_tts",
    "flush_playback",
    "call_ended",
}


def frame_category(kind: FrameKind | str) -> FrameCategory:
    try:
        return FRAME_CATEGORIES[kind]  # type: ignore[index]
    except KeyError:
        return "system"


def frame_is_session_ordered(frame: Frame) -> bool:
    return frame_category(frame.kind) in SESSION_ORDERED_CATEGORIES


def frame_is_cancellation(frame: Frame) -> bool:
    return frame.kind in CANCELLATION_FRAME_KINDS


def scope_from_frame(frame: Frame) -> ExecutionScope:
    return ExecutionScope(
        workspace_id=str(frame.data.get("workspace_id") or ""),
        voicebot_id=str(frame.data.get("voicebot_id") or ""),
        session_id=str(frame.data.get("session_id") or frame.call_id or ""),
        call_id=frame.call_id,
    )


def ids_from_frame(frame: Frame) -> ExecutionIds:
    return ExecutionIds(
        frame_id=frame.frame_id,
        turn_id=optional_int(frame.data.get("turn_id")),
        request_event_id=optional_int(frame.data.get("request_event_id")),
        response_to_event_id=optional_int(frame.data.get("response_to_event_id")),
        request_frame_id=str(frame.data.get("request_frame_id") or frame.data.get("transcript_frame_id") or ""),
        response_to_frame_id=str(frame.data.get("response_to_frame_id") or ""),
        external_task_id=str(frame.data.get("external_task_id") or frame.data.get("task_id") or frame.data.get("issue_id") or ""),
        trace_id=frame.trace_id or "",
    )


def sort_frames_for_session(frames: list[Frame]) -> list[Frame]:
    return sorted(frames, key=FrameOrderingKey.from_frame)


def optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
