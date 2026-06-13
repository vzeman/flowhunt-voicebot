from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
import queue
import socket
import threading
import time
from typing import TYPE_CHECKING
import uuid

import numpy as np

from .audio import (
    CALL_SAMPLE_RATE,
    MSG_DTMF,
    MSG_SLIN8,
    MSG_TERMINATE,
    MSG_UUID,
    float32_to_pcm16_bytes,
    pcm16_bytes_to_float32,
    read_audiosocket_message,
    rms,
    write_audiosocket_message,
)
from .call_actor import CallActorCoordinator
from .call_recording import SpeechOnlyCallRecorder
from .call_state import CallStateStore
from .config import Settings
from .events import EventStore, VoicebotEvent
from .frames import AudioInputFrame, ErrorFrame, TextFrame, TranscriptionFrame
from .pipeline import PipelineRunner
from .pipeline_contract import PIPELINE_CONTRACT_VERSION
from .processor_registry import ProcessorDependencies, ProcessorRegistry, ProcessorSpec, default_processor_registry
from .realtime_audio import AudioJitterBuffer, JitterBufferConfig, TurnDetector, trim_trailing_silence, turn_detection_config_from_settings
from .realtime_quality import metric_latency_budget_seconds
from .spoken_text import limit_spoken_response_text, split_spoken_response_text
from .speculative_turns import SpeculativeTurnCoordinator
from .transcript_filter import should_drop_agent_transcript
from .turn_coalescing import TurnCoalescer
from .transports import ASTERISK_AUDIOSOCKET_CAPABILITIES, StaticMediaTransport

if TYPE_CHECKING:
    from .stt import STTProvider
    from .tts import TTSProvider


@dataclass(frozen=True)
class AgentResponse:
    call_id: str
    text: str
    response_to_event_id: int | None = None
    response_kind: str | None = None
    partial: bool = False
    chat: dict[str, object] | None = None

    def event_data(self, spoken_text: str | None = None) -> dict[str, object]:
        data: dict[str, object] = {
            "text": spoken_text if spoken_text is not None else self.text,
            "response_to_event_id": self.response_to_event_id,
            "response_kind": self.response_kind,
            "partial": self.partial,
        }
        if self.chat is not None:
            data["chat"] = self.chat
        return data


DEFAULT_STT_PIPELINE = (ProcessorSpec("stt"), ProcessorSpec("agent-request"))
DEFAULT_TTS_PIPELINE = (ProcessorSpec("tts"),)


def _optional_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _seconds_since_timestamp(timestamp: str) -> float | None:
    if not timestamp:
        return None
    try:
        started = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - started.astimezone(UTC)).total_seconds())


def _seconds_between_timestamps(start_timestamp: str, end_timestamp: str) -> float | None:
    if not start_timestamp or not end_timestamp:
        return None
    try:
        started = datetime.fromisoformat(start_timestamp.replace("Z", "+00:00"))
        ended = datetime.fromisoformat(end_timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    if ended.tzinfo is None:
        ended = ended.replace(tzinfo=UTC)
    return max(0.0, (ended.astimezone(UTC) - started.astimezone(UTC)).total_seconds())


def _first_event_for_turn(events: list[VoicebotEvent], event_type: str, turn_id: int) -> VoicebotEvent | None:
    for event in events:
        if event.type == event_type and _optional_int(event.data.get("turn_id")) == turn_id:
            return event
    return None


class PlaybackBuffer:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queue: deque[tuple[np.ndarray, dict[str, object]]] = deque()
        self._current: np.ndarray | None = None
        self._current_data: dict[str, object] = {}
        self._position = 0
        self._generation = 0
        self._playing = False

    def interrupt(self) -> bool:
        with self._lock:
            was_playing = self._playing or self._current is not None or bool(self._queue)
            self._queue.clear()
            self._current = None
            self._current_data = {}
            self._position = 0
            self._generation += 1
            self._playing = False
            return was_playing

    def enqueue(self, audio: np.ndarray, data: dict[str, object] | None = None) -> int:
        with self._lock:
            self._queue.append((audio.astype(np.float32, copy=False).reshape(-1), dict(data or {})))
            self._generation += 1
            return self._generation

    def is_active(self) -> bool:
        with self._lock:
            return self._playing or self._current is not None or bool(self._queue)

    def active_response_event_ids(self) -> set[int]:
        with self._lock:
            ids: set[int] = set()
            for data in [self._current_data, *(item[1] for item in self._queue)]:
                try:
                    ids.add(int(data.get("response_to_event_id")))
                except (AttributeError, TypeError, ValueError):
                    continue
            return ids

    def active_response_kinds(self) -> set[str]:
        with self._lock:
            kinds: set[str] = set()
            for data in [self._current_data, *(item[1] for item in self._queue)]:
                try:
                    kind = str(data.get("response_kind") or "").strip()
                except AttributeError:
                    continue
                if kind:
                    kinds.add(kind)
            return kinds

    def next_packet(self, packet_samples: int) -> tuple[np.ndarray, bool, bool]:
        packet, started, finished, _data = self.next_packet_with_metadata(packet_samples)
        return packet, started, finished

    def next_packet_with_metadata(self, packet_samples: int) -> tuple[np.ndarray, bool, bool, dict[str, object]]:
        with self._lock:
            started = False
            finished = False
            if self._current is None and self._queue:
                self._current, self._current_data = self._queue.popleft()
                self._position = 0
                self._playing = True
                started = True

            if self._current is None:
                return np.zeros(packet_samples, dtype=np.float32), started, finished, {}

            data = dict(self._current_data)
            packet = self._current[self._position : self._position + packet_samples]
            self._position += len(packet)
            if len(packet) < packet_samples:
                packet = np.pad(packet, (0, packet_samples - len(packet)))
                self._current = None
                self._current_data = {}
                self._position = 0
                self._playing = False
                finished = True
            elif self._position >= len(self._current):
                self._current = None
                self._current_data = {}
                self._position = 0
                self._playing = False
                finished = True
            return packet, started, finished, data


class CallSession:
    def __init__(
        self,
        call_id: str,
        sock: socket.socket,
        settings: Settings,
        event_store: EventStore,
        stt: STTProvider,
        tts: TTSProvider,
        processor_registry: ProcessorRegistry | None = None,
        stt_pipeline_specs: tuple[ProcessorSpec, ...] = DEFAULT_STT_PIPELINE,
        tts_pipeline_specs: tuple[ProcessorSpec, ...] = DEFAULT_TTS_PIPELINE,
        audio_artifact_store=None,
        subagent_coordinator=None,
        subagent_lifecycle=None,
    ) -> None:
        self.call_id = call_id
        self.sock = sock
        self.settings = settings
        self.events = event_store
        self.stt = stt
        self.tts = tts
        self.descriptor = StaticMediaTransport(
            "asterisk_audiosocket",
            ASTERISK_AUDIOSOCKET_CAPABILITIES,
            sample_rate=CALL_SAMPLE_RATE,
        ).describe_session(call_id)
        self.processor_registry = processor_registry or default_processor_registry()
        processor_dependencies = ProcessorDependencies(events=event_store, stt=stt, tts=tts)
        self.stt_pipeline = PipelineRunner(
            self.processor_registry.create_many(stt_pipeline_specs, processor_dependencies)
        )
        self.tts_pipeline = PipelineRunner(
            self.processor_registry.create_many(tts_pipeline_specs, processor_dependencies)
        )
        self.playback = PlaybackBuffer()
        self.actors = CallActorCoordinator(call_id)
        self.recorder = (
            SpeechOnlyCallRecorder(
                call_id,
                audio_artifact_store,
                silence_threshold=settings.call_recording_silence_threshold,
            )
            if settings.call_recording_enabled
            else None
        )
        self._turn_coalescer = TurnCoalescer(
            call_id=lambda: self.call_id,
            events=self.events,
            emit_request=self._emit_agent_request,
            can_delay_or_merge=self._can_delay_or_merge_turns,
            window_seconds=settings.turn_coalesce_window_ms / 1000,
            max_chars=settings.turn_coalesce_max_chars,
        )
        self._speculative_turns = SpeculativeTurnCoordinator(
            settings=settings,
            events=self.events,
            subagent_coordinator=subagent_coordinator,
            subagent_lifecycle=subagent_lifecycle,
            scope_resolver=self._subagent_scope_for_call,
        )
        self.stop_event = threading.Event()
        self.recording_event = threading.Event()
        self._ignore_input_until = 0.0
        self._ignore_input_lock = threading.Lock()
        self._interrupt_generation = 0
        self._interrupt_generation_lock = threading.Lock()
        self._response_generation_lock = threading.Lock()
        self._response_generations: dict[int, int] = {}
        self._response_request_times: dict[int, float] = {}
        self._first_playback_metric_event_ids: set[int] = set()
        self._startup_response_event_ids: set[int] = set()
        self._startup_playback_guard = False
        self._last_persistent_response: dict[str, object] | None = None
        self._pending_persistent_resume: dict[str, object] | None = None
        self._speech_jobs: queue.Queue[tuple[int, np.ndarray, int]] = queue.Queue()
        self._partial_stt_jobs: queue.Queue[tuple[int, np.ndarray, int]] = queue.Queue()
        self._partial_stt_last_queued_at: dict[int, float] = {}
        self._partial_stt_last_text: dict[int, str] = {}
        self._partial_stt_inflight: set[int] = set()
        self._partial_stt_lock = threading.Lock()
        self._active_turn = 0
        self._active_turn_lock = threading.Lock()
        self._call_id_change_callback = None
        self._turn_detector = TurnDetector(turn_detection_config_from_settings(settings, CALL_SAMPLE_RATE))
        self._jitter_buffer = (
            AudioJitterBuffer(
                JitterBufferConfig(
                    sample_rate=CALL_SAMPLE_RATE,
                    frame_ms=settings.packet_ms,
                    target_delay_ms=settings.audiosocket_jitter_target_delay_ms,
                    max_delay_ms=settings.audiosocket_jitter_max_delay_ms,
                )
            )
            if settings.audiosocket_jitter_buffer_enabled
            else None
        )

    def set_call_id_change_callback(self, callback) -> None:
        self._call_id_change_callback = callback

    def run(self) -> None:
        sender = threading.Thread(target=self._send_loop, daemon=True)
        speech_worker = threading.Thread(target=self._speech_worker_loop, daemon=True)
        partial_stt_worker = threading.Thread(target=self._partial_stt_worker_loop, daemon=True)
        sender.start()
        speech_worker.start()
        partial_stt_worker.start()
        try:
            self._receive_loop()
        finally:
            self.stop_event.set()
            self._turn_coalescer.flush(reason="call_ended")
            if self._jitter_buffer is not None:
                self._jitter_buffer.clear()
            sender.join(timeout=1.0)
            speech_worker.join(timeout=1.0)
            partial_stt_worker.join(timeout=1.0)
            self._finalize_recording()
            self.events.append(self.call_id, "call_ended", {})

    def submit_agent_response(self, response: AgentResponse) -> VoicebotEvent:
        text = limit_spoken_response_text(response.text, self.settings.max_reply_chars)
        is_stream_chunk = response.partial and response.response_kind == "stream_chunk"
        self._record_agent_latency(response.response_to_event_id, stream_chunk=is_stream_chunk)
        event_type = "agent_response_partial" if response.partial else "agent_response_received"
        event = self.events.append(
            self.call_id,
            event_type,
            response.event_data(spoken_text=text),
        )
        startup_response = self._is_startup_response(response.response_to_event_id)
        persistent_response = self._is_persistent_response(response)
        bypass_speaking_deferral = self._bypasses_speaking_deferral(response)
        self._interrupt_progress_for_priority_response(response)
        if response.response_kind == "progress_ack" and "progress_ack" in self.playback.active_response_kinds():
            self.events.append(
                self.call_id,
                "agent_response_dropped",
                {
                    "reason": "duplicate_active_progress_ack",
                    "response_to_event_id": response.response_to_event_id,
                },
            )
            return event
        if not startup_response and not persistent_response and self._has_active_persistent_response():
            self.events.append(
                self.call_id,
                "agent_response_dropped",
                {
                    "reason": "active_colleague_result_playback",
                    "response_to_event_id": response.response_to_event_id,
                },
            )
            return event
        if self._has_newer_user_activity(response.response_to_event_id) and not startup_response and not persistent_response:
            self.events.append(
                self.call_id,
                "agent_response_dropped",
                {
                    "reason": "stale_response_after_new_caller_speech",
                    "response_to_event_id": response.response_to_event_id,
                },
            )
            return event

        if self.recording_event.is_set() and not startup_response and not bypass_speaking_deferral:
            self._defer_until_caller_silence(response.response_to_event_id, "caller_is_speaking")
            if not self.recording_event.is_set() and (
                persistent_response or not self._has_newer_user_activity(response.response_to_event_id)
            ):
                return self.submit_agent_response(response)
            self.events.append(
                self.call_id,
                "agent_response_dropped",
                {
                    "reason": "caller_is_speaking",
                    "response_to_event_id": response.response_to_event_id,
                },
            )
            return event

        request_generation = self._response_generation(response.response_to_event_id)
        if (
            request_generation != self._current_interrupt_generation()
            and self._has_newer_user_activity(response.response_to_event_id)
            and not startup_response
            and not persistent_response
        ):
            self.events.append(
                self.call_id,
                "agent_response_dropped",
                {
                    "reason": "stale_response_after_new_caller_speech",
                    "response_to_event_id": response.response_to_event_id,
                },
            )
            return event

        tts_synthesis_started = time.monotonic()
        first_audio_recorded = False
        queued = False
        duration = 0.0
        self.events.append(
            self.call_id,
            "tts_started",
            {
                "text": text,
                "response_to_event_id": response.response_to_event_id,
                "response_kind": response.response_kind,
            },
        )
        try:
            for chunk, chunk_duration in self._tts_audio_chunks(text):
                if not first_audio_recorded:
                    first_audio_recorded = True
                    first_audio_latency = time.monotonic() - tts_synthesis_started
                    self._record_metric(
                        "tts_first_audio_latency_seconds",
                        first_audio_latency,
                        {"response_to_event_id": response.response_to_event_id},
                    )
                    if is_stream_chunk:
                        self._record_metric(
                            "tts_stream_first_audio_latency_seconds",
                            first_audio_latency,
                            {"response_to_event_id": response.response_to_event_id},
                        )
                    self._record_response_request_to_first_playback(response.response_to_event_id)
                duration += float(chunk_duration)
                if self._has_newer_user_activity(response.response_to_event_id) and not startup_response and not persistent_response:
                    self.events.append(
                        self.call_id,
                        "agent_response_dropped",
                        {
                            "reason": "stale_response_after_new_caller_speech",
                            "response_to_event_id": response.response_to_event_id,
                        },
                    )
                    return event
                if (
                    self.recording_event.is_set() or request_generation != self._current_interrupt_generation()
                ) and not startup_response and not bypass_speaking_deferral:
                    if not queued and self.recording_event.is_set():
                        self._defer_until_caller_silence(response.response_to_event_id, "caller_started_speaking_during_tts")
                    if (
                        queued
                        or self.recording_event.is_set()
                        or (
                            request_generation != self._current_interrupt_generation()
                            and self._has_newer_user_activity(response.response_to_event_id)
                            and not persistent_response
                        )
                    ):
                        self.events.append(
                            self.call_id,
                            "agent_response_dropped",
                            {
                                "reason": "caller_started_speaking_during_tts_or_after_request",
                                "response_to_event_id": response.response_to_event_id,
                            },
                        )
                        return event
                self.playback.enqueue(
                    chunk,
                    {
                        "response_to_event_id": response.response_to_event_id,
                        "response_kind": response.response_kind,
                    },
                )
                if persistent_response:
                    self._last_persistent_response = {
                        "text": text,
                        "response_to_event_id": response.response_to_event_id,
                        "response_kind": response.response_kind,
                    }
                elif not queued:
                    self._pending_persistent_resume = None
                if startup_response and self.recording_event.is_set():
                    self._startup_playback_guard = True
                if not queued:
                    queued = True
                    self.events.append(
                        self.call_id,
                        "agent_response_queued",
                        {
                            "duration": float(chunk_duration),
                            "response_to_event_id": response.response_to_event_id,
                            "streaming": True,
                        },
                    )
        except Exception as exc:
            self.events.append(
                self.call_id,
                "tts_failed",
                {"error": str(exc), "response_to_event_id": response.response_to_event_id},
            )
            raise
        if not queued:
            self.events.append(
                self.call_id,
                "tts_failed",
                {"error": "TTS produced no audio", "response_to_event_id": response.response_to_event_id},
            )
            raise RuntimeError("TTS produced no audio")
        self._record_metric(
            "tts_synthesis_latency_seconds",
            time.monotonic() - tts_synthesis_started,
            {"response_to_event_id": response.response_to_event_id},
        )
        self._record_metric(
            "tts_duration_seconds",
            duration,
            {"response_to_event_id": response.response_to_event_id},
        )
        self.events.append(
            self.call_id,
            "tts_finished",
            {"duration": duration, "response_to_event_id": response.response_to_event_id},
        )
        self._unprotect_startup_response(response.response_to_event_id)
        return event

    def submit_user_text(self, text: str, metadata: dict[str, object] | None = None) -> tuple[VoicebotEvent, VoicebotEvent]:
        normalized = text.strip()
        if not normalized:
            raise ValueError("text is required")
        turn_id = self._new_turn()
        self._mark_interrupted("text_input")
        if self.playback.interrupt():
            self.events.append(self.call_id, "bot_playback_interrupted", {"reason": "text_input", "turn_id": turn_id})
        transcript = self.events.append(
            self.call_id,
            "user_transcript",
            {
                "turn_id": turn_id,
                "text": normalized,
                "source": "text_input",
                "stale": False,
                **(metadata or {}),
            },
        )
        request = self._emit_agent_request(
            {
                "turn_id": turn_id,
                "text": normalized,
                "trigger_event_id": transcript.id,
                "reason": "text_input",
            }
        )
        return transcript, request

    def _tts_audio_chunks(self, text: str):
        synthesize_stream = getattr(self.tts, "synthesize_stream", None)
        streaming_capable = bool(getattr(self.tts, "streaming_capable", synthesize_stream is not None))
        for chunk_text in split_spoken_response_text(text, self.settings.tts_chunk_chars):
            if synthesize_stream is None or not streaming_capable:
                yield self.tts.synthesize(chunk_text)
            else:
                yield from synthesize_stream(chunk_text)

    def interrupt_playback(self, reason: str = "agent_requested") -> VoicebotEvent:
        self._remember_interrupted_persistent_response(reason)
        interrupted = self.playback.interrupt()
        self._mark_interrupted(reason)
        self.actors.cancel("tts_playback", reason=reason)
        return self.events.append(
            self.call_id,
            "bot_playback_interrupted",
            {"reason": reason, "interrupted": interrupted},
        )

    def snapshot(self) -> dict:
        return {
            "call_id": self.call_id,
            "recording": self.recording_event.is_set(),
            "playback_active": self.playback.is_active(),
            "stopped": self.stop_event.is_set(),
            "active_turn": self._current_turn(),
            "transport": self.descriptor.transport,
            "pipeline_version": PIPELINE_CONTRACT_VERSION,
            "jitter_buffer": self._jitter_buffer_snapshot(),
            "route": self.descriptor.route.as_event_data(),
            "capabilities": {
                "call_control": sorted(self.descriptor.capabilities.call_control),
                "modalities": self.descriptor.capabilities.modalities.to_dict(),
            },
            "actors": self.actors.snapshot(),
        }

    def _jitter_buffer_snapshot(self) -> dict:
        if self._jitter_buffer is None:
            return {"enabled": False, "buffered_ms": 0, "buffered_samples": 0}
        return {
            "enabled": True,
            "buffered_ms": self._jitter_buffer.buffered_ms(),
            "buffered_samples": self._jitter_buffer.buffered_samples(),
            "target_delay_ms": self._jitter_buffer.config.target_delay_ms,
            "max_delay_ms": self._jitter_buffer.config.max_delay_ms,
        }

    def _receive_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                msg_type, payload = read_audiosocket_message(self.sock)
            except EOFError:
                return

            if msg_type == MSG_TERMINATE:
                return
            if msg_type == MSG_UUID:
                audiosocket_uuid = str(uuid.UUID(bytes=payload)) if len(payload) == 16 else payload.hex()
                old_call_id = self.call_id
                self.call_id = audiosocket_uuid
                if self.recorder is not None:
                    self.recorder.update_call_id(self.call_id)
                self.actors.update_call_id(self.call_id)
                self.descriptor = StaticMediaTransport(
                    "asterisk_audiosocket",
                    ASTERISK_AUDIOSOCKET_CAPABILITIES,
                    sample_rate=CALL_SAMPLE_RATE,
                ).describe_session(self.call_id, {"external_call_id": audiosocket_uuid, "audiosocket_uuid": audiosocket_uuid})
                if self._call_id_change_callback is not None:
                    self._call_id_change_callback(old_call_id, self)
                lifecycle_data = {
                    **self.descriptor.lifecycle_event_data(),
                    "pipeline_version": PIPELINE_CONTRACT_VERSION,
                }
                self.events.append(self.call_id, "call_started", lifecycle_data)
                connected = self.events.append(
                    self.call_id,
                    "call_connected",
                    lifecycle_data,
                )
                if self.settings.greet_on_connect:
                    request = self.events.append(
                        self.call_id,
                        "agent_response_requested",
                        {
                            "reason": "call_connected",
                            "trigger_event_id": connected.id,
                            "text": self.settings.connect_greeting_prompt,
                        },
                    )
                    self._remember_response_generation(request.id)
                    self._protect_startup_response(request.id)
                continue
            if msg_type == MSG_DTMF:
                self.events.append(self.call_id, "dtmf", {"digit": payload.decode(errors="replace")})
                continue
            if msg_type != MSG_SLIN8:
                self.events.append(self.call_id, "system", {"message": f"ignored audiosocket message {msg_type}"})
                continue

            block = pcm16_bytes_to_float32(payload)
            self.process_remote_audio_block(block)

    def process_remote_audio_block(self, block: np.ndarray) -> int:
        if self._jitter_buffer is None:
            self._process_audio_block(block)
            return 1 if block.size else 0
        self._jitter_buffer.push(block)
        processed = 0
        while True:
            frame = self._jitter_buffer.pop()
            if frame is None:
                break
            self._process_audio_block(frame)
            processed += 1
        return processed

    def _process_audio_block(self, block: np.ndarray) -> None:
        if block.size == 0 or self.stop_event.is_set():
            return
        block = block.astype(np.float32, copy=False).reshape(-1)
        result = self._turn_detector.process_block(
            block,
            playback_active=self.playback.is_active(),
            echo_suppressed=self._echo_tail_active(),
        )
        if result.started:
            self.recording_event.set()
            turn_id = self._new_turn()
            self.actors.started("audio_input", correlation_id=str(turn_id), reason="speech_started")
            self.events.append(self.call_id, "user_speech_started", {"turn_id": turn_id, "level": result.level})
            self._record_vad_decision(result.decision, result.level, result.block_ms, {"turn_id": turn_id})
            self._mark_interrupted("user_speech_started")

            if result.interrupt_playback and self.playback.is_active():
                self._remember_interrupted_persistent_response("user_speech_started")
            if result.interrupt_playback and self.playback.interrupt():
                self.actors.cancel("tts_playback", reason="user_speech_started", correlation_id=str(turn_id))
                self.events.append(self.call_id, "bot_playback_interrupted", {"reason": "user_speech_started"})
            return

        if not result.finished:
            if result.decision == "speech_continues":
                self._maybe_queue_partial_stt(self._current_turn())
            return

        self.recording_event.clear()
        turn_id = self._current_turn()
        self.actors.completed("audio_input", correlation_id=str(turn_id), reason="speech_finished")
        self.events.append(self.call_id, "user_speech_finished", {"turn_id": turn_id, "duration": result.duration})
        self._record_metric("speech_duration_seconds", result.duration, {"turn_id": turn_id})
        self._record_metric("silence_duration_seconds", result.silence_ms / 1000, {"turn_id": turn_id})
        self._record_vad_decision(
            result.decision,
            result.level,
            result.block_ms,
            {"turn_id": turn_id, "duration": result.duration},
        )
        if result.decision == "speech_finished" and result.audio is not None:
            audio = trim_trailing_silence(
                result.audio,
                sample_rate=CALL_SAMPLE_RATE,
                threshold=self.settings.stop_threshold,
            )
            trimmed_seconds = max(0.0, (len(result.audio) - len(audio)) / CALL_SAMPLE_RATE)
            if trimmed_seconds > 0:
                self._record_metric("stt_audio_trimmed_seconds", trimmed_seconds, {"turn_id": turn_id})
            if self.recorder is not None:
                self.recorder.append_speech(
                    "caller",
                    audio,
                    CALL_SAMPLE_RATE,
                    metadata={"turn_id": turn_id},
                )
            self.actors.queued("stt", correlation_id=str(turn_id), reason="speech_finished")
            self._speech_jobs.put((turn_id, audio, self._current_interrupt_generation()))

    def _maybe_queue_partial_stt(self, turn_id: int) -> None:
        if not self.settings.stt_partial_enabled or turn_id <= 0:
            return
        now = time.monotonic()
        with self._partial_stt_lock:
            if turn_id in self._partial_stt_inflight:
                return
            last_queued_at = self._partial_stt_last_queued_at.get(turn_id, 0.0)
            if now - last_queued_at < self.settings.stt_partial_interval_seconds:
                return
            audio = self._turn_detector.current_audio()
            if len(audio) / CALL_SAMPLE_RATE < self.settings.stt_partial_min_seconds:
                return
            self._partial_stt_inflight.add(turn_id)
            self._partial_stt_last_queued_at[turn_id] = now
        self.actors.queued("stt", correlation_id=str(turn_id), reason="partial_snapshot")
        self._partial_stt_jobs.put((turn_id, audio, self._current_interrupt_generation()))

    def _partial_stt_worker_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                turn_id, audio, turn_generation = self._partial_stt_jobs.get(timeout=0.2)
            except queue.Empty:
                continue

            self.actors.started("stt", correlation_id=str(turn_id), reason="partial_snapshot")
            try:
                started = time.perf_counter()
                result = self.stt.transcribe(audio, CALL_SAMPLE_RATE)
                elapsed = time.perf_counter() - started
                if turn_generation == self._current_interrupt_generation() and result.text:
                    self._emit_partial_transcript(turn_id, result.text, elapsed, result.metadata or {})
            except Exception as exc:
                self.events.append(
                    self.call_id,
                    "stt_failed",
                    {"turn_id": turn_id, "error": str(exc), "partial": True},
                )
            finally:
                with self._partial_stt_lock:
                    self._partial_stt_inflight.discard(turn_id)
                self.actors.completed("stt", correlation_id=str(turn_id), reason="partial_snapshot_done")

    def _emit_partial_transcript(self, turn_id: int, text: str, elapsed: float, metadata: dict) -> None:
        text = text.strip()
        if len(text) < self.settings.stt_partial_min_chars:
            return
        with self._partial_stt_lock:
            if self._partial_stt_last_text.get(turn_id) == text:
                return
            self._partial_stt_last_text[turn_id] = text
        event = self.events.append(
            self.call_id,
            "user_transcript_partial",
            {
                "turn_id": turn_id,
                "text": text,
                "elapsed": elapsed,
                "metadata": {**metadata, "source": "partial_snapshot"},
            },
        )
        self._record_turn_event_latency(
            "partial_stt_first_text_seconds",
            start_event_type="user_speech_started",
            end_event=event,
            data={"turn_id": turn_id, "partial_event_id": event.id},
            first_only=True,
        )
        self._speculative_turns.observe_partial(event)

    def _speech_worker_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                turn_id, audio, turn_generation = self._speech_jobs.get(timeout=0.2)
            except queue.Empty:
                continue

            self.events.append(self.call_id, "stt_started", {"turn_id": turn_id})
            stt_started_recorded = True
            self.actors.started("stt", correlation_id=str(turn_id), reason="speech_job")
            try:
                frames = asyncio.run(
                    self.stt_pipeline.push(
                        AudioInputFrame(
                            self.call_id,
                            audio,
                            CALL_SAMPLE_RATE,
                            data={"turn_id": turn_id},
                        )
                    )
                )
            except Exception as exc:
                self.events.append(self.call_id, "stt_failed", {"turn_id": turn_id, "error": str(exc)})
                self.actors.completed("stt", correlation_id=str(turn_id), reason="stt_failed")
                continue
            transcript_event_ids: dict[str, int] = {}
            stale_turn = turn_generation != self._current_interrupt_generation()
            transcription_frame_seen = False
            error_frame_seen = False
            for frame in frames:
                if not isinstance(frame, TranscriptionFrame):
                    if isinstance(frame, ErrorFrame):
                        error_frame_seen = True
                        self.events.append(
                            self.call_id,
                            "stt_failed",
                            {
                                "turn_id": turn_id,
                                "error": frame.error,
                                "processor": frame.data.get("processor"),
                            },
                        )
                    if isinstance(frame, TextFrame) and frame.kind == "agent_request":
                        transcript_frame_id = str(frame.data.get("transcript_frame_id", ""))
                        transcript_event_id = transcript_event_ids.get(transcript_frame_id)
                        drop_decision = should_drop_agent_transcript(
                            frame.text,
                            stale=stale_turn,
                            min_chars=self.settings.agent_min_transcript_chars,
                            min_tokens=self.settings.agent_min_transcript_tokens,
                            audio_duration_seconds=len(audio) / CALL_SAMPLE_RATE,
                            audio_rms=rms(audio),
                        )
                        if drop_decision.should_drop:
                            self.events.append(
                                self.call_id,
                                "stt_result_dropped",
                                {
                                    "turn_id": frame.data.get("turn_id"),
                                    "transcript_event_id": transcript_event_id,
                                    "text": frame.text,
                                    "reason": drop_decision.reason,
                                    **(
                                        {"stale": True, "stale_reason": "newer_caller_speech_started"}
                                        if stale_turn
                                        else {}
                                    ),
                                },
                            )
                            self._maybe_resume_interrupted_persistent_response(str(drop_decision.reason))
                            self._speculative_turns.cancel_turn(
                                _optional_int(frame.data.get("turn_id")),
                                reason=str(drop_decision.reason),
                            )
                            continue
                        self._turn_coalescer.handle(
                            {
                                "turn_id": frame.data.get("turn_id"),
                                "transcript_event_id": transcript_event_id,
                                "text": frame.text,
                            }
                        )
                    continue

                transcription_frame_seen = True
                if frame.kind == "transcription_started" and not stt_started_recorded:
                    self.events.append(self.call_id, "stt_started", {"turn_id": frame.turn_id})
                elif frame.kind == "transcription_partial":
                    event = self.events.append(
                        self.call_id,
                        "user_transcript_partial",
                        {
                            "turn_id": frame.turn_id,
                            "text": frame.text,
                            "elapsed": frame.data.get("elapsed"),
                            "metadata": frame.metadata,
                        },
                    )
                    self._record_turn_event_latency(
                        "partial_stt_first_text_seconds",
                        start_event_type="user_speech_started",
                        end_event=event,
                        data={"turn_id": frame.turn_id, "partial_event_id": event.id},
                        first_only=True,
                    )
                elif frame.kind == "transcription_empty":
                    self._record_metric(
                        "stt_duration_seconds",
                        float(frame.data.get("elapsed") or 0.0),
                        {"turn_id": frame.turn_id, "result": "empty"},
                    )
                    self.events.append(
                        self.call_id,
                        "stt_no_text",
                        {
                            "turn_id": frame.turn_id,
                            "elapsed": frame.data.get("elapsed"),
                            "reason": frame.data.get("reason", "empty_result"),
                            "metadata": frame.metadata,
                        },
                    )
                    self._maybe_resume_interrupted_persistent_response("stt_no_text")
                    self._speculative_turns.cancel_turn(frame.turn_id, reason="stt_no_text")
                elif frame.kind == "transcription_finished":
                    self._record_metric(
                        "stt_duration_seconds",
                        float(frame.data.get("elapsed") or 0.0),
                        {"turn_id": frame.turn_id, "result": "text"},
                    )
                    self.events.append(
                        self.call_id,
                        "stt_finished",
                        {
                            "turn_id": frame.turn_id,
                            "elapsed": frame.data.get("elapsed"),
                            "metadata": frame.metadata,
                        },
                    )
                elif frame.kind == "user_transcript":
                    transcript = self.events.append(
                        self.call_id,
                        "user_transcript",
                        {
                            "turn_id": frame.turn_id,
                            "text": frame.text,
                            "elapsed": frame.data.get("elapsed"),
                            "stale": stale_turn,
                        },
                    )
                    transcript_event_ids[frame.frame_id] = transcript.id
                    self._record_turn_event_latency(
                        "speech_finished_to_final_transcript_seconds",
                        start_event_type="user_speech_finished",
                        end_event=transcript,
                        data={"turn_id": frame.turn_id, "transcript_event_id": transcript.id},
                    )
            if not transcription_frame_seen and not error_frame_seen:
                self.events.append(
                    self.call_id,
                    "stt_failed",
                    {"turn_id": turn_id, "error": "STT pipeline produced no transcription frames"},
                )
            self.actors.completed("stt", correlation_id=str(turn_id), reason="stt_done")

    def _send_loop(self) -> None:
        packet_samples = max(1, int(CALL_SAMPLE_RATE * self.settings.packet_ms / 1000))
        packet_seconds = self.settings.packet_ms / 1000
        while not self.stop_event.is_set():
            if self.recording_event.is_set():
                if self._startup_playback_guard and self.playback.is_active():
                    packet = np.zeros(packet_samples, dtype=np.float32)
                    try:
                        write_audiosocket_message(self.sock, MSG_SLIN8, float32_to_pcm16_bytes(packet))
                    except OSError:
                        self.stop_event.set()
                        return
                    time.sleep(packet_seconds)
                    continue
                if self.playback.is_active():
                    self._remember_interrupted_persistent_response("caller_is_speaking")
                if self.playback.interrupt():
                    self._mark_interrupted("caller_is_speaking")
                    self.actors.cancel("tts_playback", reason="caller_is_speaking")
                    self.events.append(self.call_id, "bot_playback_interrupted", {"reason": "caller_is_speaking"})
                packet = np.zeros(packet_samples, dtype=np.float32)
                try:
                    write_audiosocket_message(self.sock, MSG_SLIN8, float32_to_pcm16_bytes(packet))
                except OSError:
                    self.stop_event.set()
                    return
                time.sleep(packet_seconds)
                continue

            packet, started, finished, playback_data = self.playback.next_packet_with_metadata(packet_samples)
            if started:
                self._startup_playback_guard = False
                self._set_echo_tail(self.settings.echo_tail_ms)
                self.events.append(self.call_id, "bot_playback_started", playback_data)
            if self.recorder is not None:
                self.recorder.append_speech(
                    "voicebot",
                    packet,
                    CALL_SAMPLE_RATE,
                    metadata=dict(playback_data),
                )
            try:
                write_audiosocket_message(self.sock, MSG_SLIN8, float32_to_pcm16_bytes(packet))
            except OSError:
                self.stop_event.set()
                return
            if finished:
                self._set_echo_tail(self.settings.echo_tail_ms)
                self.events.append(self.call_id, "bot_playback_finished", playback_data)
                self._forget_finished_persistent_response(playback_data)
            time.sleep(packet_seconds)

    def _new_turn(self) -> int:
        with self._active_turn_lock:
            self._active_turn += 1
            return self._active_turn

    def _current_turn(self) -> int:
        with self._active_turn_lock:
            return self._active_turn

    def _set_echo_tail(self, tail_ms: int) -> None:
        with self._ignore_input_lock:
            self._ignore_input_until = max(self._ignore_input_until, time.monotonic() + tail_ms / 1000)

    def _echo_tail_active(self) -> bool:
        with self._ignore_input_lock:
            return time.monotonic() < self._ignore_input_until

    def _mark_interrupted(self, reason: str) -> int:
        with self._interrupt_generation_lock:
            self._interrupt_generation += 1
            return self._interrupt_generation

    def _current_interrupt_generation(self) -> int:
        with self._interrupt_generation_lock:
            return self._interrupt_generation

    def _remember_response_generation(self, event_id: int) -> None:
        with self._response_generation_lock:
            self._response_generations[event_id] = self._current_interrupt_generation()
            self._response_request_times[event_id] = time.monotonic()

    def _protect_startup_response(self, event_id: int) -> None:
        with self._response_generation_lock:
            self._startup_response_event_ids.add(event_id)

    def _unprotect_startup_response(self, event_id: int | None) -> None:
        if event_id is None:
            return
        with self._response_generation_lock:
            self._startup_response_event_ids.discard(event_id)

    def _is_startup_response(self, event_id: int | None) -> bool:
        if event_id is None:
            return False
        with self._response_generation_lock:
            return event_id in self._startup_response_event_ids

    def _response_generation(self, event_id: int | None) -> int:
        if event_id is None:
            return self._current_interrupt_generation()
        with self._response_generation_lock:
            return self._response_generations.get(event_id, self._current_interrupt_generation())

    def _record_agent_latency(self, event_id: int | None, *, stream_chunk: bool = False) -> None:
        if event_id is None:
            return
        with self._response_generation_lock:
            started = self._response_request_times.pop(event_id, None)
        if started is not None:
            elapsed = time.monotonic() - started
            self._record_metric("agent_response_latency_seconds", elapsed, {"event_id": event_id})
            if stream_chunk:
                self._record_metric("agent_stream_first_text_latency_seconds", elapsed, {"event_id": event_id})

    def _record_response_request_to_first_playback(self, event_id: int | None) -> None:
        if event_id is None:
            return
        with self._response_generation_lock:
            if event_id in self._first_playback_metric_event_ids:
                return
            self._first_playback_metric_event_ids.add(event_id)
        source = self.events.get_event(event_id)
        elapsed = _seconds_since_timestamp(source.timestamp if source else "")
        if elapsed is not None:
            self._record_metric("response_request_to_first_playback_seconds", elapsed, {"event_id": event_id})

    def _record_turn_event_latency(
        self,
        name: str,
        *,
        start_event_type: str,
        end_event: VoicebotEvent,
        data: dict,
        first_only: bool = False,
    ) -> None:
        turn_id = _optional_int(end_event.data.get("turn_id"))
        if turn_id is None:
            return
        if first_only and any(
            event.type == "metrics"
            and event.data.get("name") == name
            and _optional_int(event.data.get("turn_id")) == turn_id
            for event in self.events.list_events(call_id=self.call_id, limit=1000)
        ):
            return
        start = _first_event_for_turn(self.events.list_events(call_id=self.call_id, limit=1000), start_event_type, turn_id)
        elapsed = _seconds_between_timestamps(start.timestamp if start else "", end_event.timestamp)
        if elapsed is not None:
            self._record_metric(name, elapsed, data)

    def _record_metric(self, name: str, value: float, data: dict | None = None) -> None:
        payload = {"name": name, "value": value, **(data or {})}
        budget = metric_latency_budget_seconds(self.settings, name)
        if budget is not None:
            payload["budget_seconds"] = budget
        metric = self.events.append(self.call_id, "metrics", payload)
        if budget is not None and value > budget:
            self.events.append(
                self.call_id,
                "latency_budget_exceeded",
                {
                    "metric_event_id": metric.id,
                    "name": name,
                    "value": value,
                    "budget_seconds": budget,
                    **(data or {}),
                },
            )

    def _emit_agent_request(self, data: dict) -> VoicebotEvent:
        request = self.events.append(self.call_id, "agent_response_requested", data)
        self._remember_response_generation(request.id)
        self._speculative_turns.reconcile_final_request(
            turn_id=_optional_int(data.get("turn_id")),
            final_text=str(data.get("text") or ""),
            final_request_event_id=request.id,
        )
        return request

    def _subagent_scope_for_call(self, call_id: str) -> dict[str, str] | None:
        route = self.descriptor.route
        workspace_id = route.workspace_id or self.settings.flowhunt_workspace_id
        if not workspace_id:
            return None
        voicebot_id = route.voicebot_id or self.settings.default_voicebot_id or "default"
        return {
            "workspace_id": workspace_id,
            "voicebot_id": voicebot_id,
            "session_id": call_id,
            "call_id": call_id,
            "transport": self.descriptor.transport,
        }

    def _can_delay_or_merge_turns(self) -> bool:
        return not self.playback.is_active()

    def _record_vad_decision(self, decision: str, level: float, block_ms: int, data: dict | None = None) -> None:
        self._record_metric(
            "vad_decision",
            1.0,
            {
                "decision": decision,
                "level": level,
                "block_ms": block_ms,
                "sample_rate": CALL_SAMPLE_RATE,
                "transport": self.descriptor.transport,
                **(data or {}),
            },
        )

    def _finalize_recording(self) -> None:
        if self.recorder is None:
            return
        metadata = self.recorder.finalize()
        if metadata is not None:
            self.events.append(self.call_id, "call_recording_saved", metadata)

    def _should_defer_response(self, event_id: int | None) -> bool:
        if event_id is None:
            return False
        request = self.events.get_event(event_id)
        return request is not None and request.type == "agent_response_requested" and request.data.get("reason") == "colleague_result"

    def _is_persistent_response(self, response: AgentResponse) -> bool:
        return response.response_kind in {"call_control_ack", "colleague_result"} or self._should_defer_response(
            response.response_to_event_id
        )

    def _bypasses_speaking_deferral(self, response: AgentResponse) -> bool:
        return response.response_kind == "call_control_ack"

    def _interrupt_progress_for_priority_response(self, response: AgentResponse) -> None:
        if response.response_kind != "call_control_ack" or "progress_ack" not in self.playback.active_response_kinds():
            return
        interrupted = self.playback.interrupt()
        self.events.append(
            self.call_id,
            "bot_playback_interrupted",
            {
                "reason": "call_control_ack_priority",
                "interrupted": interrupted,
                "response_to_event_id": response.response_to_event_id,
            },
        )

    def _has_active_persistent_response(self) -> bool:
        return bool({"call_control_ack", "colleague_result"} & self.playback.active_response_kinds()) or any(
            self._should_defer_response(event_id) for event_id in self.playback.active_response_event_ids()
        )

    def _remember_interrupted_persistent_response(self, reason: str) -> None:
        if not self._has_active_persistent_response() or self._last_persistent_response is None:
            return
        self._pending_persistent_resume = dict(self._last_persistent_response)
        self.events.append(
            self.call_id,
            "agent_response_deferred",
            {
                "reason": "persistent_response_interrupted",
                "interrupt_reason": reason,
                "response_to_event_id": self._pending_persistent_resume.get("response_to_event_id"),
            },
        )

    def _maybe_resume_interrupted_persistent_response(self, reason: str) -> None:
        pending = self._pending_persistent_resume
        if pending is None or self.recording_event.is_set() or self.playback.is_active():
            return
        self._pending_persistent_resume = None
        if reason != "stt_no_text":
            self.events.append(
                self.call_id,
                "agent_response_dropped",
                {
                    "reason": "interrupted_persistent_response_not_resumed",
                    "interrupt_result": reason,
                    "response_to_event_id": _optional_int(pending.get("response_to_event_id")),
                },
            )
            return
        response = AgentResponse(
            self.call_id,
            str(pending.get("text") or ""),
            response_to_event_id=_optional_int(pending.get("response_to_event_id")),
            response_kind=str(pending.get("response_kind") or "colleague_result"),
        )
        self.events.append(
            self.call_id,
            "agent_response_resumed",
            {"reason": reason, "response_to_event_id": response.response_to_event_id},
        )
        self.submit_agent_response(response)

    def _forget_finished_persistent_response(self, playback_data: dict[str, object]) -> None:
        if str(playback_data.get("response_kind") or "") not in {"call_control_ack", "colleague_result"}:
            return
        self._last_persistent_response = None
        self._pending_persistent_resume = None

    def _has_newer_user_transcript(self, event_id: int | None) -> bool:
        if event_id is None:
            return False
        return any(
            event.id > event_id and event.type == "user_transcript"
            for event in self.events.list_events(call_id=self.call_id, limit=200)
        )

    def _has_newer_user_activity(self, event_id: int | None) -> bool:
        if event_id is None:
            return False
        return any(
            event.id > event_id and event.type in {"user_speech_started", "user_transcript"}
            for event in self.events.list_events(call_id=self.call_id, limit=200)
        )

    def _defer_until_caller_silence(self, event_id: int | None, reason: str) -> None:
        wait_seconds = max(0.0, self.settings.deferred_response_wait_seconds)
        self.events.append(
            self.call_id,
            "agent_response_deferred",
            {
                "reason": reason,
                "response_to_event_id": event_id,
                "wait_seconds": wait_seconds,
            },
        )
        deadline = time.monotonic() + wait_seconds
        while self.recording_event.is_set() and not self.stop_event.is_set() and time.monotonic() < deadline:
            time.sleep(0.05)


class CallRegistry:
    def __init__(self, state_store: CallStateStore | None = None) -> None:
        self._lock = threading.Lock()
        self._calls: dict[str, CallSession] = {}
        self.state_store = state_store or CallStateStore()

    def add(self, session: CallSession) -> None:
        with self._lock:
            self._calls[session.call_id] = session
        self.state_store.upsert(session.snapshot())

    def replace_id(self, old_call_id: str, session: CallSession) -> None:
        with self._lock:
            self._calls.pop(old_call_id, None)
            self._calls[session.call_id] = session
        if old_call_id != session.call_id:
            self.state_store.end(old_call_id)
        self.state_store.upsert(session.snapshot())

    def remove(self, call_id: str) -> None:
        with self._lock:
            self._calls.pop(call_id, None)
        self.state_store.end(call_id)

    def stop(self, call_id: str) -> bool:
        with self._lock:
            session = self._calls.pop(call_id, None)
        if session is None:
            return False
        stop = getattr(session, "stop", None)
        if stop is not None:
            stop()
        else:
            session.stop_event.set()
        self.state_store.end(call_id)
        return True

    def get(self, call_id: str) -> CallSession | None:
        with self._lock:
            return self._calls.get(call_id)

    def active_call_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._calls)

    def snapshot(self, call_id: str) -> dict | None:
        with self._lock:
            session = self._calls.get(call_id)
        if session is None:
            return None
        snapshot = session.snapshot()
        self.state_store.upsert(snapshot)
        return snapshot

    def snapshots(self) -> list[dict]:
        with self._lock:
            sessions = list(self._calls.values())
        snapshots = [session.snapshot() for session in sessions]
        for snapshot in snapshots:
            self.state_store.upsert(snapshot)
        return sorted(snapshots, key=lambda item: item["call_id"])

    def stored_snapshots(self, active_only: bool = False) -> list[dict]:
        return list(self.state_store.list(active_only=active_only))
