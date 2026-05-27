from __future__ import annotations

import time
from typing import Protocol

from .audio import CALL_SAMPLE_RATE
from .events import EventStore
from .frames import (
    AudioInputFrame,
    AudioOutputFrame,
    ErrorFrame,
    Frame,
    PlaybackFrame,
    TextFrame,
    TranscriptionFrame,
)
from .pipeline import PipelineContext
from .processors import FrameProcessorBase, ProcessorResult


class STTService(Protocol):
    def transcribe(self, call_audio):
        raise NotImplementedError


class TTSService(Protocol):
    def synthesize(self, text: str):
        raise NotImplementedError


FRAME_EVENT_TYPES = {
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
    "error": "system",
    "system": "system",
}


class EventLogProcessor(FrameProcessorBase):
    def __init__(self, events: EventStore) -> None:
        super().__init__("event-log")
        self.events = events

    def handle(self, frame: Frame, context: PipelineContext) -> Frame:
        event_type = FRAME_EVENT_TYPES.get(frame.kind)
        if event_type is not None:
            self.events.append(frame.call_id, event_type, frame.data)
        return frame


class STTProcessor(FrameProcessorBase):
    def __init__(self, stt: STTService) -> None:
        super().__init__("stt")
        self.stt = stt

    def handle(self, frame: Frame, context: PipelineContext) -> ProcessorResult:
        if not isinstance(frame, AudioInputFrame) or "turn_id" not in frame.data:
            return frame

        turn_id = int(frame.data["turn_id"])
        started = TranscriptionFrame("transcription_started", frame.call_id, turn_id, trace_id=frame.trace_id)
        start_time = time.perf_counter()
        result = self.stt.transcribe(frame.audio)
        elapsed = time.perf_counter() - start_time
        metadata = result.metadata or {}
        if not result.text:
            empty = TranscriptionFrame(
                "transcription_empty",
                frame.call_id,
                turn_id,
                metadata=metadata,
                trace_id=frame.trace_id,
                data={"elapsed": elapsed, "reason": result.reason or "empty_result"},
            )
            return [started, empty]

        finished = TranscriptionFrame(
            "transcription_finished",
            frame.call_id,
            turn_id,
            metadata=metadata,
            trace_id=frame.trace_id,
            data={"elapsed": elapsed},
        )
        transcript = TranscriptionFrame(
            "user_transcript",
            frame.call_id,
            turn_id,
            text=result.text,
            trace_id=frame.trace_id,
            data={"elapsed": elapsed},
        )
        return [started, finished, transcript]


class AgentRequestProcessor(FrameProcessorBase):
    def __init__(self) -> None:
        super().__init__("agent-request")

    def handle(self, frame: Frame, context: PipelineContext) -> ProcessorResult:
        if not isinstance(frame, TranscriptionFrame) or frame.kind != "user_transcript":
            return frame
        return [
            frame,
            TextFrame(
                "agent_request",
                frame.call_id,
                frame.text,
                trace_id=frame.trace_id,
                data={"turn_id": frame.turn_id, "transcript_frame_id": frame.frame_id},
            ),
        ]


class TTSProcessor(FrameProcessorBase):
    def __init__(self, tts: TTSService) -> None:
        super().__init__("tts")
        self.tts = tts

    def handle(self, frame: Frame, context: PipelineContext) -> ProcessorResult:
        if not isinstance(frame, TextFrame) or frame.kind != "agent_response":
            return frame

        started = TextFrame(
            "tts_started",
            frame.call_id,
            frame.text,
            response_to_frame_id=frame.response_to_frame_id,
            trace_id=frame.trace_id,
        )
        try:
            audio, duration = self.tts.synthesize(frame.text)
        except Exception as exc:
            failed = TextFrame(
                "tts_failed",
                frame.call_id,
                str(exc),
                response_to_frame_id=frame.response_to_frame_id,
                trace_id=frame.trace_id,
            )
            return [frame, started, failed, ErrorFrame(frame.call_id, str(exc), trace_id=frame.trace_id)]

        output = AudioOutputFrame(
            frame.call_id,
            audio,
            CALL_SAMPLE_RATE,
            trace_id=frame.trace_id,
            data={"duration": duration, "response_to_frame_id": frame.response_to_frame_id},
        )
        finished = PlaybackFrame(
            "tts_finished",
            frame.call_id,
            trace_id=frame.trace_id,
            data={"duration": duration, "response_to_frame_id": frame.response_to_frame_id},
        )
        return [frame, started, finished, output]
