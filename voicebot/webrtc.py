from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import queue
import threading
import time
import uuid
from typing import TYPE_CHECKING, Any

import numpy as np
from scipy.io import wavfile

from .audio import CALL_SAMPLE_RATE, STT_SAMPLE_RATE, resample_audio, rms
from .calls import AgentResponse, DEFAULT_STT_PIPELINE, DEFAULT_TTS_PIPELINE, PlaybackBuffer
from .config import Settings
from .events import EventStore, VoicebotEvent
from .frames import AudioInputFrame, TextFrame, TranscriptionFrame
from .pipeline import PipelineRunner
from .pipeline_contract import PIPELINE_CONTRACT_VERSION
from .processor_registry import ProcessorDependencies, ProcessorRegistry, ProcessorSpec, default_processor_registry
from .realtime_audio import AudioJitterBuffer, JitterBufferConfig, TurnDetector, trim_trailing_silence, turn_detection_config_from_settings
from .spoken_text import limit_spoken_response_text, split_spoken_response_text
from .transports import WEBRTC_CAPABILITIES, StaticMediaTransport
from .workspace_model import VoicebotSessionRecord, VoicebotSessionStore

try:
    from aiortc import MediaStreamTrack, RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
    from av import AudioFrame
except ModuleNotFoundError:
    MediaStreamTrack = object
    RTCConfiguration = None
    RTCIceServer = None
    RTCPeerConnection = None
    RTCSessionDescription = None
    AudioFrame = None

if TYPE_CHECKING:
    from .calls import CallRegistry
    from .stt import STTProvider
    from .tts import TTSProvider


@dataclass(frozen=True)
class WebRTCSessionSnapshot:
    call_id: str
    session_id: str
    connection_state: str
    recording: bool
    playback_active: bool
    stopped: bool
    active_turn: int


class WebRTCAudioOutputTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self, session: "WebRTCCallSession") -> None:
        super().__init__()
        self.session = session
        self.packet_samples = max(1, int(CALL_SAMPLE_RATE * session.settings.packet_ms / 1000))
        self.packet_seconds = session.settings.packet_ms / 1000
        self._timestamp = 0

    async def recv(self):
        await asyncio.sleep(self.packet_seconds)
        packet, started, finished, playback_data = self.session.next_playback_packet(self.packet_samples)
        if started:
            self.session.events.append(self.session.call_id, "bot_playback_started", playback_data)
        if finished:
            self.session.events.append(self.session.call_id, "bot_playback_finished", playback_data)

        frame = AudioFrame(format="s16", layout="mono", samples=self.packet_samples)
        frame.sample_rate = CALL_SAMPLE_RATE
        frame.pts = self._timestamp
        frame.time_base = fractions_time_base(CALL_SAMPLE_RATE)
        self._timestamp += self.packet_samples
        frame.planes[0].update((np.clip(packet, -1.0, 1.0) * 32767.0).astype("<i2").tobytes())
        return frame


class WebRTCCallSession:
    def __init__(
        self,
        call_id: str,
        session_id: str,
        settings: Settings,
        event_store: EventStore,
        stt: "STTProvider",
        tts: "TTSProvider",
        processor_registry: ProcessorRegistry | None = None,
        stt_pipeline_specs: tuple[ProcessorSpec, ...] = DEFAULT_STT_PIPELINE,
        tts_pipeline_specs: tuple[ProcessorSpec, ...] = DEFAULT_TTS_PIPELINE,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.call_id = call_id
        self.session_id = session_id
        self.settings = settings
        self.events = event_store
        self.stt = stt
        self.tts = tts
        self.metadata = metadata or {}
        self.descriptor = StaticMediaTransport(
            "webrtc",
            WEBRTC_CAPABILITIES,
            sample_rate=STT_SAMPLE_RATE,
        ).describe_session(call_id, {"session_id": session_id, **self.metadata})
        self.processor_registry = processor_registry or default_processor_registry()
        processor_dependencies = ProcessorDependencies(events=event_store, stt=stt, tts=tts)
        self.stt_pipeline = PipelineRunner(
            self.processor_registry.create_many(stt_pipeline_specs, processor_dependencies)
        )
        self.tts_pipeline = PipelineRunner(
            self.processor_registry.create_many(tts_pipeline_specs, processor_dependencies)
        )
        self.playback = PlaybackBuffer()
        self.stop_event = threading.Event()
        self.recording_event = threading.Event()
        self.connection_state = "new"
        self._ignore_input_until = 0.0
        self._ignore_input_lock = threading.Lock()
        self._interrupt_generation = 0
        self._interrupt_generation_lock = threading.Lock()
        self._response_generation_lock = threading.Lock()
        self._response_generations: dict[int, int] = {}
        self._response_request_times: dict[int, float] = {}
        self._startup_response_event_ids: set[int] = set()
        self._startup_playback_guard = False
        self._speech_jobs: queue.Queue[tuple[int, np.ndarray, int]] = queue.Queue()
        self._active_turn = 0
        self._active_turn_lock = threading.Lock()
        self._turn_detector = TurnDetector(turn_detection_config_from_settings(settings, STT_SAMPLE_RATE))
        self._jitter_buffer = (
            AudioJitterBuffer(
                JitterBufferConfig(
                    sample_rate=STT_SAMPLE_RATE,
                    frame_ms=settings.packet_ms,
                    target_delay_ms=settings.webrtc_jitter_target_delay_ms,
                    max_delay_ms=settings.webrtc_jitter_max_delay_ms,
                )
            )
            if settings.webrtc_jitter_buffer_enabled
            else None
        )
        self._speech_worker = threading.Thread(target=self._speech_worker_loop, daemon=True)
        self._speech_worker.start()

    def start(self) -> None:
        lifecycle_data = {
            "session_id": self.session_id,
            **self.descriptor.lifecycle_event_data(),
            "pipeline_version": PIPELINE_CONTRACT_VERSION,
        }
        self.events.append(
            self.call_id,
            "call_started",
            lifecycle_data,
        )
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

    async def receive_track(self, track) -> None:
        try:
            while not self.stop_event.is_set():
                frame = await track.recv()
                self.process_remote_audio_block(audio_frame_to_call_audio(frame))
        except Exception as exc:
            if not self.stop_event.is_set():
                self.events.append(self.call_id, "transport_error", {"transport": "webrtc", "error": str(exc)})
                self.stop()

    def process_remote_audio_block(self, block: np.ndarray) -> int:
        if self._jitter_buffer is None:
            self.process_audio_block(block)
            return 1 if block.size else 0
        self._jitter_buffer.push(block)
        processed = 0
        while True:
            frame = self._jitter_buffer.pop()
            if frame is None:
                break
            self.process_audio_block(frame)
            processed += 1
        return processed

    def process_audio_block(self, block: np.ndarray) -> None:
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
            self.events.append(self.call_id, "user_speech_started", {"turn_id": turn_id, "level": result.level})
            self._record_vad_decision(result.decision, result.level, result.block_ms, {"turn_id": turn_id})
            self._mark_interrupted("user_speech_started")
            if result.interrupt_playback and self.playback.interrupt():
                self.events.append(self.call_id, "bot_playback_interrupted", {"reason": "user_speech_started"})
            return

        if not result.finished:
            return

        self.recording_event.clear()
        turn_id = self._current_turn()
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
                sample_rate=STT_SAMPLE_RATE,
                threshold=self.settings.stop_threshold,
            )
            trimmed_seconds = max(0.0, (len(result.audio) - len(audio)) / STT_SAMPLE_RATE)
            if trimmed_seconds > 0:
                self._record_metric("stt_audio_trimmed_seconds", trimmed_seconds, {"turn_id": turn_id})
            self._speech_jobs.put((turn_id, audio, self._current_interrupt_generation()))

    def submit_agent_response(self, response: AgentResponse) -> VoicebotEvent:
        text = limit_spoken_response_text(response.text, self.settings.max_reply_chars)
        self._record_agent_latency(response.response_to_event_id)
        event = self.events.append(
            self.call_id,
            "agent_response_received",
            {
                "text": text,
                "response_to_event_id": response.response_to_event_id,
                "response_kind": response.response_kind,
            },
        )
        startup_response = self._is_startup_response(response.response_to_event_id)
        persistent_response = self._is_persistent_response(response)
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

        if self.recording_event.is_set() and not startup_response:
            self._defer_until_caller_silence(response.response_to_event_id, "caller_is_speaking")
            if not self.recording_event.is_set() and (
                persistent_response or not self._has_newer_user_activity(response.response_to_event_id)
            ):
                return self.submit_agent_response(response)
            self.events.append(
                self.call_id,
                "agent_response_dropped",
                {"reason": "caller_is_speaking", "response_to_event_id": response.response_to_event_id},
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
                    self._record_metric(
                        "tts_first_audio_latency_seconds",
                        time.monotonic() - tts_synthesis_started,
                        {"response_to_event_id": response.response_to_event_id},
                    )
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
                if (self.recording_event.is_set() or request_generation != self._current_interrupt_generation()) and not startup_response:
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

    def _tts_audio_chunks(self, text: str):
        synthesize_stream = getattr(self.tts, "synthesize_stream", None)
        for chunk_text in split_spoken_response_text(text, self.settings.tts_chunk_chars):
            if synthesize_stream is None:
                yield self.tts.synthesize(chunk_text)
            else:
                yield from synthesize_stream(chunk_text)

    def interrupt_playback(self, reason: str = "agent_requested") -> VoicebotEvent:
        interrupted = self.playback.interrupt()
        self._mark_interrupted(reason)
        return self.events.append(
            self.call_id,
            "bot_playback_interrupted",
            {"reason": reason, "interrupted": interrupted},
        )

    def next_playback_packet(self, packet_samples: int) -> tuple[np.ndarray, bool, bool, dict[str, object]]:
        if self.recording_event.is_set():
            if self._startup_playback_guard and self.playback.is_active():
                return np.zeros(packet_samples, dtype=np.float32), False, False, {}
            if self.playback.interrupt():
                self._mark_interrupted("caller_is_speaking")
                self.events.append(self.call_id, "bot_playback_interrupted", {"reason": "caller_is_speaking"})
            return np.zeros(packet_samples, dtype=np.float32), False, False, {}
        packet, started, finished, playback_data = self.playback.next_packet_with_metadata(packet_samples)
        if started or finished:
            if started:
                self._startup_playback_guard = False
            self._set_echo_tail(self.settings.echo_tail_ms)
        return packet, started, finished, playback_data

    def snapshot(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "session_id": self.session_id,
            "transport": "webrtc",
            "pipeline_version": PIPELINE_CONTRACT_VERSION,
            "connection_state": self.connection_state,
            "recording": self.recording_event.is_set(),
            "playback_active": self.playback.is_active(),
            "stopped": self.stop_event.is_set(),
            "active_turn": self._current_turn(),
            "jitter_buffer": self._jitter_buffer_snapshot(),
            "route": self.descriptor.route.as_event_data(),
            "capabilities": {
                "call_control": sorted(self.descriptor.capabilities.call_control),
                "modalities": self.descriptor.capabilities.modalities.to_dict(),
            },
            "metadata": self.metadata,
        }

    def stop(self) -> None:
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        if self._jitter_buffer is not None:
            self._jitter_buffer.clear()
        self.connection_state = "closed"
        self.events.append(
            self.call_id,
            "call_ended",
            {
                "session_id": self.session_id,
                **self.descriptor.lifecycle_event_data(),
                "pipeline_version": PIPELINE_CONTRACT_VERSION,
            },
        )

    def _jitter_buffer_snapshot(self) -> dict[str, Any]:
        if self._jitter_buffer is None:
            return {"enabled": False, "buffered_ms": 0, "buffered_samples": 0}
        return {
            "enabled": True,
            "buffered_ms": self._jitter_buffer.buffered_ms(),
            "buffered_samples": self._jitter_buffer.buffered_samples(),
            "target_delay_ms": self._jitter_buffer.config.target_delay_ms,
            "max_delay_ms": self._jitter_buffer.config.max_delay_ms,
        }

    def _speech_worker_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                turn_id, audio, turn_generation = self._speech_jobs.get(timeout=0.2)
            except queue.Empty:
                continue

            debug_path = self._capture_debug_audio(turn_id, audio)
            if debug_path:
                self.events.append(
                    self.call_id,
                    "debug_audio_captured",
                    {
                        "turn_id": turn_id,
                        "path": debug_path,
                        "sample_rate": STT_SAMPLE_RATE,
                        "samples": int(len(audio)),
                        "duration": len(audio) / STT_SAMPLE_RATE,
                        "rms": rms(audio),
                        "peak": float(np.max(np.abs(audio), initial=0.0)),
                    },
                )

            frames = asyncio.run(
                self.stt_pipeline.push(
                    AudioInputFrame(
                        self.call_id,
                        audio,
                        STT_SAMPLE_RATE,
                        data={"turn_id": turn_id},
                    )
                )
            )
            transcript_event_ids: dict[str, int] = {}
            stale_turn = turn_generation != self._current_interrupt_generation()
            for frame in frames:
                if not isinstance(frame, TranscriptionFrame):
                    if isinstance(frame, TextFrame) and frame.kind == "agent_request":
                        transcript_frame_id = str(frame.data.get("transcript_frame_id", ""))
                        request = self.events.append(
                            self.call_id,
                            "agent_response_requested",
                            {
                                **({"reason": "stale_transcript"} if stale_turn else {}),
                                "turn_id": frame.data.get("turn_id"),
                                "transcript_event_id": transcript_event_ids.get(transcript_frame_id),
                                "text": frame.text,
                                **({"stale": True, "stale_reason": "newer_caller_speech_started"} if stale_turn else {}),
                            },
                        )
                        self._remember_response_generation(request.id)
                    continue

                if frame.kind == "transcription_started":
                    self.events.append(self.call_id, "stt_started", {"turn_id": frame.turn_id})
                elif frame.kind == "transcription_partial":
                    self.events.append(
                        self.call_id,
                        "user_transcript_partial",
                        {
                            "turn_id": frame.turn_id,
                            "text": frame.text,
                            "elapsed": frame.data.get("elapsed"),
                            "metadata": frame.metadata,
                        },
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

    def _record_agent_latency(self, event_id: int | None) -> None:
        if event_id is None:
            return
        with self._response_generation_lock:
            started = self._response_request_times.pop(event_id, None)
        if started is not None:
            self._record_metric("agent_response_latency_seconds", time.monotonic() - started, {"event_id": event_id})

    def _record_metric(self, name: str, value: float, data: dict | None = None) -> None:
        self.events.append(self.call_id, "metrics", {"name": name, "value": value, **(data or {})})

    def _record_vad_decision(self, decision: str, level: float, block_ms: int, data: dict | None = None) -> None:
        self._record_metric(
            "vad_decision",
            1.0,
            {
                "decision": decision,
                "level": level,
                "block_ms": block_ms,
                "sample_rate": STT_SAMPLE_RATE,
                "transport": "webrtc",
                **(data or {}),
            },
        )

    def _capture_debug_audio(self, turn_id: int, audio: np.ndarray) -> str | None:
        if not self.settings.debug_audio_capture:
            return None
        directory = Path(self.settings.debug_audio_dir)
        directory.mkdir(parents=True, exist_ok=True)
        filename = f"{self.call_id}_turn-{turn_id}.wav".replace("/", "_")
        path = directory / filename
        wavfile.write(path, STT_SAMPLE_RATE, np.clip(audio, -1.0, 1.0))
        return str(path)

    def _should_defer_response(self, event_id: int | None) -> bool:
        if event_id is None:
            return False
        request = self.events.get_event(event_id)
        return request is not None and request.type == "agent_response_requested" and request.data.get("reason") == "colleague_result"

    def _is_persistent_response(self, response: AgentResponse) -> bool:
        return response.response_kind in {"call_control_ack", "colleague_result"} or self._should_defer_response(
            response.response_to_event_id
        )

    def _has_active_persistent_response(self) -> bool:
        return bool({"call_control_ack", "colleague_result"} & self.playback.active_response_kinds()) or any(
            self._should_defer_response(event_id) for event_id in self.playback.active_response_event_ids()
        )

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


class WebRTCSessionManager:
    def __init__(
        self,
        settings: Settings,
        events: EventStore,
        registry: "CallRegistry",
        stt: "STTProvider",
        tts: "TTSProvider",
        stt_pipeline_specs: tuple[ProcessorSpec, ...],
        tts_pipeline_specs: tuple[ProcessorSpec, ...],
        session_store: VoicebotSessionStore | None = None,
    ) -> None:
        self.settings = settings
        self.events = events
        self.registry = registry
        self.stt = stt
        self.tts = tts
        self.stt_pipeline_specs = stt_pipeline_specs
        self.tts_pipeline_specs = tts_pipeline_specs
        self.session_store = session_store
        self._lock = asyncio.Lock()
        self._sessions: dict[str, tuple[Any, WebRTCCallSession]] = {}

    def available(self) -> bool:
        return RTCPeerConnection is not None

    async def create_session(self, offer_sdp: str, offer_type: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.available():
            raise RuntimeError("aiortc is not installed")
        session_id = str(uuid.uuid4())
        call_id = f"webrtc-{session_id}"
        pc = RTCPeerConnection(configuration=self._rtc_configuration())
        session = WebRTCCallSession(
            call_id=call_id,
            session_id=session_id,
            settings=self.settings,
            event_store=self.events,
            stt=self.stt,
            tts=self.tts,
            stt_pipeline_specs=self.stt_pipeline_specs,
            tts_pipeline_specs=self.tts_pipeline_specs,
            metadata=metadata,
        )
        self.registry.add(session)
        pc.addTrack(WebRTCAudioOutputTrack(session))

        @pc.on("track")
        def on_track(track) -> None:
            if track.kind == "audio":
                asyncio.create_task(session.receive_track(track))

        @pc.on("connectionstatechange")
        async def on_connectionstatechange() -> None:
            session.connection_state = pc.connectionState
            self.events.append(
                session.call_id,
                "system",
                {"message": "webrtc_connection_state", "state": pc.connectionState, "session_id": session_id},
            )
            if pc.connectionState in {"failed", "closed", "disconnected"}:
                await self.close_session(session_id)

        await pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type=offer_type))
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        async with self._lock:
            self._sessions[session_id] = (pc, session)
        session.start()
        self._persist_session_started(session)
        return {
            "session_id": session_id,
            "call_id": call_id,
            "answer": {
                "sdp": pc.localDescription.sdp,
                "type": pc.localDescription.type,
            },
        }

    async def close_session(self, session_id: str) -> bool:
        async with self._lock:
            pair = self._sessions.pop(session_id, None)
        if pair is None:
            return False
        pc, session = pair
        session.stop()
        self._persist_session_ended(session)
        self.registry.remove(session.call_id)
        await pc.close()
        return True

    async def close_call(self, call_id: str) -> bool:
        async with self._lock:
            session_id = next(
                (
                    candidate
                    for candidate, (_pc, session) in self._sessions.items()
                    if session.call_id == call_id
                ),
                None,
            )
        if session_id is None:
            return False
        return await self.close_session(session_id)

    def snapshots(self) -> list[dict[str, Any]]:
        return [
            {
                "session_id": session_id,
                **session.snapshot(),
            }
            for session_id, (_pc, session) in sorted(self._sessions.items())
        ]

    def _rtc_configuration(self):
        if not self.settings.webrtc_stun_urls:
            return None
        servers = [RTCIceServer(urls=url) for url in self.settings.webrtc_stun_urls]
        return RTCConfiguration(iceServers=servers)

    def _persist_session_started(self, session: WebRTCCallSession) -> None:
        if self.session_store is None:
            return
        route = session.descriptor.route
        if not route.workspace_id or not route.voicebot_id:
            return
        self.session_store.save(
            VoicebotSessionRecord(
                session_id=session.session_id,
                workspace_id=route.workspace_id,
                voicebot_id=route.voicebot_id,
                channel_id=_optional_metadata_str(route.metadata.get("channel_id")),
                external_session_id=session.call_id,
                metadata={"transport": "webrtc", **route.metadata},
            )
        )

    def _persist_session_ended(self, session: WebRTCCallSession) -> None:
        if self.session_store is None:
            return
        route = session.descriptor.route
        if not route.workspace_id:
            return
        try:
            self.session_store.end(session.session_id, route.workspace_id)
        except KeyError:
            return


def _optional_metadata_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def audio_frame_to_call_audio(frame) -> np.ndarray:
    samples = np.asarray(frame.to_ndarray())
    original_dtype = samples.dtype
    channel_count = audio_frame_channel_count(frame)
    frame_samples = int(getattr(frame, "samples", 0) or 0)
    if samples.ndim == 2 and samples.shape[0] == 1 and channel_count > 1:
        packed = samples.reshape(-1)
        if frame_samples > 0 and packed.size >= frame_samples * channel_count:
            packed = packed[: frame_samples * channel_count]
        if packed.size % channel_count == 0:
            samples = packed.reshape(-1, channel_count).mean(axis=1)
        else:
            samples = packed
    elif samples.ndim > 1:
        channel_axis = 0 if samples.shape[0] == channel_count else samples.ndim - 1
        samples = samples.mean(axis=channel_axis)
    if original_dtype.kind in {"i", "u"}:
        max_value = float(np.iinfo(original_dtype).max)
        samples = samples.astype(np.float32) / max_value
    else:
        samples = samples.astype(np.float32)
        if np.max(np.abs(samples), initial=0.0) > 1.0:
            samples /= 32768.0
    sample_rate = int(getattr(frame, "sample_rate", CALL_SAMPLE_RATE) or CALL_SAMPLE_RATE)
    return resample_audio(samples.reshape(-1), sample_rate, STT_SAMPLE_RATE)


def audio_frame_channel_count(frame) -> int:
    layout = getattr(frame, "layout", None)
    channels = getattr(layout, "channels", None)
    if channels is None:
        return 1
    try:
        return max(1, len(channels))
    except TypeError:
        try:
            return max(1, int(channels))
        except (TypeError, ValueError):
            return 1


def fractions_time_base(sample_rate: int):
    from fractions import Fraction

    return Fraction(1, sample_rate)
