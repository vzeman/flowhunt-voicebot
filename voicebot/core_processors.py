from __future__ import annotations

import time
from typing import Protocol
import inspect

from .audio import CALL_SAMPLE_RATE
from .events import EventStore
from .execution_model import ids_from_frame, scope_from_frame
from .frame_events import frame_to_event_data, frame_to_event_type
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
    def transcribe(self, call_audio, sample_rate: int = CALL_SAMPLE_RATE):
        raise NotImplementedError

    def transcribe_stream(self, call_audio, sample_rate: int = CALL_SAMPLE_RATE):
        raise NotImplementedError


class TTSService(Protocol):
    def synthesize(self, text: str):
        raise NotImplementedError

    def synthesize_stream(self, text: str):
        raise NotImplementedError


class EventLogProcessor(FrameProcessorBase):
    def __init__(self, events: EventStore) -> None:
        super().__init__("event-log")
        self.events = events

    def handle(self, frame: Frame, context: PipelineContext) -> Frame:
        event_type = frame_to_event_type(frame)
        if event_type is not None:
            self.events.append_scoped(scope_from_frame(frame), event_type, frame_to_event_data(frame), ids_from_frame(frame))
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
        output: list[Frame] = [started]
        final_seen = False
        transcribe_stream = getattr(self.stt, "transcribe_stream", None)
        if transcribe_stream:
            results = call_stt_method(transcribe_stream, frame.audio, frame.sample_rate)
        else:
            results = [call_stt_method(self.stt.transcribe, frame.audio, frame.sample_rate)]
        for result in results:
            elapsed = time.perf_counter() - start_time
            metadata = result.metadata or {}
            is_final = getattr(result, "is_final", True)
            if not result.text:
                if is_final:
                    output.append(
                        TranscriptionFrame(
                            "transcription_empty",
                            frame.call_id,
                            turn_id,
                            metadata=metadata,
                            trace_id=frame.trace_id,
                            data={"elapsed": elapsed, "reason": result.reason or "empty_result"},
                        )
                    )
                    final_seen = True
                continue

            if not is_final:
                output.append(
                    TranscriptionFrame(
                        "transcription_partial",
                        frame.call_id,
                        turn_id,
                        text=result.text,
                        metadata=metadata,
                        trace_id=frame.trace_id,
                        data={"elapsed": elapsed},
                    )
                )
                continue

            output.append(
                TranscriptionFrame(
                    "transcription_finished",
                    frame.call_id,
                    turn_id,
                    metadata=metadata,
                    trace_id=frame.trace_id,
                    data={"elapsed": elapsed},
                )
            )
            output.append(
                TranscriptionFrame(
                    "user_transcript",
                    frame.call_id,
                    turn_id,
                    text=result.text,
                    trace_id=frame.trace_id,
                    data={"elapsed": elapsed},
                )
            )
            final_seen = True
        if not final_seen:
            elapsed = time.perf_counter() - start_time
            output.append(
                TranscriptionFrame(
                    "transcription_empty",
                    frame.call_id,
                    turn_id,
                    trace_id=frame.trace_id,
                    data={"elapsed": elapsed, "reason": "empty_stream"},
                )
            )
        return output


def call_stt_method(method, audio, sample_rate: int):
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        return method(audio, sample_rate)
    if len(signature.parameters) >= 2:
        return method(audio, sample_rate)
    return method(audio)


class AgentRequestProcessor(FrameProcessorBase):
    def __init__(self, request_partials: bool = False) -> None:
        super().__init__("agent-request")
        self.request_partials = request_partials

    def handle(self, frame: Frame, context: PipelineContext) -> ProcessorResult:
        if not isinstance(frame, TranscriptionFrame):
            return frame
        if frame.kind == "transcription_partial":
            if not self.request_partials:
                return frame
            return [
                frame,
                TextFrame(
                    "agent_request",
                    frame.call_id,
                    frame.text,
                    trace_id=frame.trace_id,
                    data={
                        "turn_id": frame.turn_id,
                        "transcript_frame_id": frame.frame_id,
                        "partial": True,
                    },
                ),
            ]
        if frame.kind != "user_transcript":
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
        if not isinstance(frame, TextFrame) or frame.kind not in {"agent_response", "agent_response_partial"}:
            return frame

        started = TextFrame(
            "tts_started",
            frame.call_id,
            frame.text,
            response_to_frame_id=frame.response_to_frame_id,
            trace_id=frame.trace_id,
        )
        try:
            synthesize_stream = getattr(self.tts, "synthesize_stream", None)
            chunks = synthesize_stream(frame.text) if synthesize_stream else [self.tts.synthesize(frame.text)]
            output: list[Frame] = [frame, started]
            total_duration = 0.0
            for audio, duration in chunks:
                total_duration += float(duration)
                output.append(
                    AudioOutputFrame(
                        frame.call_id,
                        audio,
                        CALL_SAMPLE_RATE,
                        trace_id=frame.trace_id,
                        data={
                            "duration": float(duration),
                            "response_to_frame_id": frame.response_to_frame_id,
                        },
                    )
                )
        except Exception as exc:
            failed = TextFrame(
                "tts_failed",
                frame.call_id,
                str(exc),
                response_to_frame_id=frame.response_to_frame_id,
                trace_id=frame.trace_id,
            )
            return [frame, started, failed, ErrorFrame(frame.call_id, str(exc), trace_id=frame.trace_id)]

        finished = PlaybackFrame(
            "tts_finished",
            frame.call_id,
            trace_id=frame.trace_id,
            data={
                "duration": total_duration,
                "response_to_frame_id": frame.response_to_frame_id,
                "partial": frame.kind == "agent_response_partial",
            },
        )
        output.append(finished)
        return output
