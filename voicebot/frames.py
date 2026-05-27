from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

import numpy as np


FrameKind = Literal[
    "audio_input",
    "audio_output",
    "call_started",
    "call_connected",
    "call_ended",
    "dtmf",
    "speech_started",
    "speech_finished",
    "transcription_started",
    "transcription_finished",
    "transcription_empty",
    "user_transcript",
    "agent_request",
    "agent_response",
    "agent_response_dropped",
    "tts_started",
    "tts_finished",
    "tts_failed",
    "playback_started",
    "playback_interrupted",
    "playback_finished",
    "call_control_requested",
    "call_control_completed",
    "interrupt",
    "cancel_agent",
    "cancel_tts",
    "pause_input",
    "resume_input",
    "flush_playback",
    "metrics",
    "error",
    "system",
]


def frame_timestamp() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class Frame:
    kind: FrameKind
    call_id: str
    frame_id: str = field(default_factory=lambda: str(uuid4()))
    timestamp: str = field(default_factory=frame_timestamp)
    trace_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AudioInputFrame(Frame):
    audio: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float32))
    sample_rate: int = 8_000

    def __init__(
        self,
        call_id: str,
        audio: np.ndarray,
        sample_rate: int,
        *,
        trace_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        object.__setattr__(self, "kind", "audio_input")
        object.__setattr__(self, "call_id", call_id)
        object.__setattr__(self, "frame_id", str(uuid4()))
        object.__setattr__(self, "timestamp", frame_timestamp())
        object.__setattr__(self, "trace_id", trace_id)
        object.__setattr__(self, "data", data or {})
        object.__setattr__(self, "audio", audio.astype(np.float32, copy=False).reshape(-1))
        object.__setattr__(self, "sample_rate", sample_rate)


@dataclass(frozen=True)
class AudioOutputFrame(Frame):
    audio: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float32))
    sample_rate: int = 8_000
    interruptible: bool = True

    def __init__(
        self,
        call_id: str,
        audio: np.ndarray,
        sample_rate: int,
        *,
        interruptible: bool = True,
        trace_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        object.__setattr__(self, "kind", "audio_output")
        object.__setattr__(self, "call_id", call_id)
        object.__setattr__(self, "frame_id", str(uuid4()))
        object.__setattr__(self, "timestamp", frame_timestamp())
        object.__setattr__(self, "trace_id", trace_id)
        object.__setattr__(self, "data", data or {})
        object.__setattr__(self, "audio", audio.astype(np.float32, copy=False).reshape(-1))
        object.__setattr__(self, "sample_rate", sample_rate)
        object.__setattr__(self, "interruptible", interruptible)


@dataclass(frozen=True)
class CallLifecycleFrame(Frame):
    def __init__(
        self,
        kind: Literal["call_started", "call_connected", "call_ended"],
        call_id: str,
        *,
        trace_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "call_id", call_id)
        object.__setattr__(self, "frame_id", str(uuid4()))
        object.__setattr__(self, "timestamp", frame_timestamp())
        object.__setattr__(self, "trace_id", trace_id)
        object.__setattr__(self, "data", data or {})


@dataclass(frozen=True)
class DTMFFrame(Frame):
    digit: str = ""

    def __init__(self, call_id: str, digit: str, *, trace_id: str | None = None) -> None:
        object.__setattr__(self, "kind", "dtmf")
        object.__setattr__(self, "call_id", call_id)
        object.__setattr__(self, "frame_id", str(uuid4()))
        object.__setattr__(self, "timestamp", frame_timestamp())
        object.__setattr__(self, "trace_id", trace_id)
        object.__setattr__(self, "digit", digit)
        object.__setattr__(self, "data", {"digit": digit})


@dataclass(frozen=True)
class SpeechFrame(Frame):
    turn_id: int = 0

    def __init__(
        self,
        kind: Literal["speech_started", "speech_finished"],
        call_id: str,
        turn_id: int,
        *,
        trace_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "call_id", call_id)
        object.__setattr__(self, "frame_id", str(uuid4()))
        object.__setattr__(self, "timestamp", frame_timestamp())
        object.__setattr__(self, "trace_id", trace_id)
        object.__setattr__(self, "turn_id", turn_id)
        object.__setattr__(self, "data", {"turn_id": turn_id, **(data or {})})


@dataclass(frozen=True)
class TranscriptionFrame(Frame):
    turn_id: int = 0
    text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __init__(
        self,
        kind: Literal["transcription_started", "transcription_finished", "transcription_empty", "user_transcript"],
        call_id: str,
        turn_id: int,
        *,
        text: str = "",
        metadata: dict[str, Any] | None = None,
        trace_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        payload = {"turn_id": turn_id, **(data or {})}
        if text:
            payload["text"] = text
        if metadata:
            payload["metadata"] = metadata
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "call_id", call_id)
        object.__setattr__(self, "frame_id", str(uuid4()))
        object.__setattr__(self, "timestamp", frame_timestamp())
        object.__setattr__(self, "trace_id", trace_id)
        object.__setattr__(self, "turn_id", turn_id)
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "metadata", metadata or {})
        object.__setattr__(self, "data", payload)


@dataclass(frozen=True)
class TextFrame(Frame):
    text: str = ""
    response_to_frame_id: str | None = None

    def __init__(
        self,
        kind: Literal[
            "agent_request",
            "agent_response",
            "agent_response_dropped",
            "tts_started",
            "tts_failed",
            "system",
        ],
        call_id: str,
        text: str,
        *,
        response_to_frame_id: str | None = None,
        trace_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        payload = {"text": text, **(data or {})}
        if response_to_frame_id:
            payload["response_to_frame_id"] = response_to_frame_id
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "call_id", call_id)
        object.__setattr__(self, "frame_id", str(uuid4()))
        object.__setattr__(self, "timestamp", frame_timestamp())
        object.__setattr__(self, "trace_id", trace_id)
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "response_to_frame_id", response_to_frame_id)
        object.__setattr__(self, "data", payload)


@dataclass(frozen=True)
class PlaybackFrame(Frame):
    def __init__(
        self,
        kind: Literal["tts_finished", "playback_started", "playback_interrupted", "playback_finished"],
        call_id: str,
        *,
        trace_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "call_id", call_id)
        object.__setattr__(self, "frame_id", str(uuid4()))
        object.__setattr__(self, "timestamp", frame_timestamp())
        object.__setattr__(self, "trace_id", trace_id)
        object.__setattr__(self, "data", data or {})


@dataclass(frozen=True)
class CallControlFrame(Frame):
    action: str = ""
    target: str | None = None

    def __init__(
        self,
        kind: Literal["call_control_requested", "call_control_completed"],
        call_id: str,
        action: str,
        *,
        target: str | None = None,
        trace_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        payload = {"action": action, **(data or {})}
        if target is not None:
            payload["target"] = target
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "call_id", call_id)
        object.__setattr__(self, "frame_id", str(uuid4()))
        object.__setattr__(self, "timestamp", frame_timestamp())
        object.__setattr__(self, "trace_id", trace_id)
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "data", payload)


ControlKind = Literal[
    "interrupt",
    "cancel_agent",
    "cancel_tts",
    "pause_input",
    "resume_input",
    "flush_playback",
]


@dataclass(frozen=True)
class ControlFrame(Frame):
    reason: str = ""

    def __init__(
        self,
        kind: ControlKind,
        call_id: str,
        *,
        reason: str = "",
        trace_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        payload = data or {}
        if reason:
            payload = {"reason": reason, **payload}
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "call_id", call_id)
        object.__setattr__(self, "frame_id", str(uuid4()))
        object.__setattr__(self, "timestamp", frame_timestamp())
        object.__setattr__(self, "trace_id", trace_id)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "data", payload)


@dataclass(frozen=True)
class MetricsFrame(Frame):
    name: str = ""
    value: float = 0.0

    def __init__(
        self,
        call_id: str,
        name: str,
        value: float,
        *,
        trace_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        object.__setattr__(self, "kind", "metrics")
        object.__setattr__(self, "call_id", call_id)
        object.__setattr__(self, "frame_id", str(uuid4()))
        object.__setattr__(self, "timestamp", frame_timestamp())
        object.__setattr__(self, "trace_id", trace_id)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "data", {"name": name, "value": value, **(data or {})})


@dataclass(frozen=True)
class ErrorFrame(Frame):
    error: str = ""

    def __init__(
        self,
        call_id: str,
        error: str,
        *,
        trace_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        object.__setattr__(self, "kind", "error")
        object.__setattr__(self, "call_id", call_id)
        object.__setattr__(self, "frame_id", str(uuid4()))
        object.__setattr__(self, "timestamp", frame_timestamp())
        object.__setattr__(self, "trace_id", trace_id)
        object.__setattr__(self, "error", error)
        object.__setattr__(self, "data", {"error": error, **(data or {})})
