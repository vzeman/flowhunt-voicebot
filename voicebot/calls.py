from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import queue
import socket
import threading
import time
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
from .config import Settings
from .events import EventStore, VoicebotEvent
from .stt import STTProvider
from .tts import TTSProvider


@dataclass(frozen=True)
class AgentResponse:
    call_id: str
    text: str
    response_to_event_id: int | None = None


class PlaybackBuffer:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queue: deque[np.ndarray] = deque()
        self._current: np.ndarray | None = None
        self._position = 0
        self._generation = 0
        self._playing = False

    def interrupt(self) -> bool:
        with self._lock:
            was_playing = self._playing or self._current is not None or bool(self._queue)
            self._queue.clear()
            self._current = None
            self._position = 0
            self._generation += 1
            self._playing = False
            return was_playing

    def enqueue(self, audio: np.ndarray) -> int:
        with self._lock:
            self._queue.append(audio.astype(np.float32, copy=False).reshape(-1))
            self._generation += 1
            return self._generation

    def next_packet(self, packet_samples: int) -> tuple[np.ndarray, bool, bool]:
        with self._lock:
            started = False
            finished = False
            if self._current is None and self._queue:
                self._current = self._queue.popleft()
                self._position = 0
                self._playing = True
                started = True

            if self._current is None:
                return np.zeros(packet_samples, dtype=np.float32), started, finished

            packet = self._current[self._position : self._position + packet_samples]
            self._position += len(packet)
            if len(packet) < packet_samples:
                packet = np.pad(packet, (0, packet_samples - len(packet)))
                self._current = None
                self._position = 0
                self._playing = False
                finished = True
            elif self._position >= len(self._current):
                self._current = None
                self._position = 0
                self._playing = False
                finished = True
            return packet, started, finished


class CallSession:
    def __init__(
        self,
        call_id: str,
        sock: socket.socket,
        settings: Settings,
        event_store: EventStore,
        stt: STTProvider,
        tts: TTSProvider,
    ) -> None:
        self.call_id = call_id
        self.sock = sock
        self.settings = settings
        self.events = event_store
        self.stt = stt
        self.tts = tts
        self.playback = PlaybackBuffer()
        self.stop_event = threading.Event()
        self._speech_jobs: queue.Queue[tuple[int, np.ndarray]] = queue.Queue()
        self._active_turn = 0
        self._active_turn_lock = threading.Lock()
        self._call_id_change_callback = None

    def set_call_id_change_callback(self, callback) -> None:
        self._call_id_change_callback = callback

    def run(self) -> None:
        sender = threading.Thread(target=self._send_loop, daemon=True)
        speech_worker = threading.Thread(target=self._speech_worker_loop, daemon=True)
        sender.start()
        speech_worker.start()
        try:
            self._receive_loop()
        finally:
            self.stop_event.set()
            sender.join(timeout=1.0)
            speech_worker.join(timeout=1.0)
            self.events.append(self.call_id, "call_ended", {})

    def submit_agent_response(self, response: AgentResponse) -> VoicebotEvent:
        event = self.events.append(
            self.call_id,
            "agent_response_received",
            {"text": response.text, "response_to_event_id": response.response_to_event_id},
        )
        audio, duration = self.tts.synthesize(response.text)
        self.playback.enqueue(audio)
        self.events.append(self.call_id, "system", {"message": "agent response queued", "duration": duration})
        return event

    def _receive_loop(self) -> None:
        is_recording = False
        collected: list[np.ndarray] = []
        silence_ms = 0
        speech_ms = 0

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
                if self._call_id_change_callback is not None:
                    self._call_id_change_callback(old_call_id, self)
                self.events.append(self.call_id, "call_started", {"audiosocket_uuid": audiosocket_uuid})
                continue
            if msg_type == MSG_DTMF:
                self.events.append(self.call_id, "dtmf", {"digit": payload.decode(errors="replace")})
                continue
            if msg_type != MSG_SLIN8:
                self.events.append(self.call_id, "system", {"message": f"ignored audiosocket message {msg_type}"})
                continue

            block = pcm16_bytes_to_float32(payload)
            block_ms = int(len(block) / CALL_SAMPLE_RATE * 1000)
            level = rms(block)

            if not is_recording:
                if level < self.settings.start_threshold:
                    continue

                if self.playback.interrupt():
                    self.events.append(self.call_id, "bot_playback_interrupted", {"reason": "user_speech_started"})

                is_recording = True
                collected = [block]
                silence_ms = 0
                speech_ms = block_ms
                turn_id = self._new_turn()
                self.events.append(self.call_id, "user_speech_started", {"turn_id": turn_id, "level": level})
                continue

            collected.append(block)
            speech_ms += block_ms
            if level < self.settings.stop_threshold:
                silence_ms += block_ms
            else:
                silence_ms = 0

            if silence_ms < self.settings.silence_ms and speech_ms < int(self.settings.max_seconds * 1000):
                continue

            audio = np.concatenate(collected)
            is_recording = False
            collected = []
            silence_ms = 0
            speech_ms = 0

            duration = len(audio) / CALL_SAMPLE_RATE
            turn_id = self._current_turn()
            self.events.append(self.call_id, "user_speech_finished", {"turn_id": turn_id, "duration": duration})
            if duration >= self.settings.min_seconds:
                self._speech_jobs.put((turn_id, audio))

    def _speech_worker_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                turn_id, audio = self._speech_jobs.get(timeout=0.2)
            except queue.Empty:
                continue

            started = time.perf_counter()
            text = self.stt.transcribe(audio)
            elapsed = time.perf_counter() - started
            if not text:
                self.events.append(self.call_id, "system", {"message": "no text recognized", "turn_id": turn_id})
                continue

            transcript = self.events.append(
                self.call_id,
                "user_transcript",
                {"turn_id": turn_id, "text": text, "elapsed": elapsed},
            )
            self.events.append(
                self.call_id,
                "agent_response_requested",
                {"turn_id": turn_id, "transcript_event_id": transcript.id, "text": text},
            )

    def _send_loop(self) -> None:
        packet_samples = max(1, int(CALL_SAMPLE_RATE * self.settings.packet_ms / 1000))
        packet_seconds = self.settings.packet_ms / 1000
        while not self.stop_event.is_set():
            packet, started, finished = self.playback.next_packet(packet_samples)
            if started:
                self.events.append(self.call_id, "bot_playback_started", {})
            try:
                write_audiosocket_message(self.sock, MSG_SLIN8, float32_to_pcm16_bytes(packet))
            except OSError:
                self.stop_event.set()
                return
            if finished:
                self.events.append(self.call_id, "bot_playback_finished", {})
            time.sleep(packet_seconds)

    def _new_turn(self) -> int:
        with self._active_turn_lock:
            self._active_turn += 1
            return self._active_turn

    def _current_turn(self) -> int:
        with self._active_turn_lock:
            return self._active_turn


class CallRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: dict[str, CallSession] = {}

    def add(self, session: CallSession) -> None:
        with self._lock:
            self._calls[session.call_id] = session

    def replace_id(self, old_call_id: str, session: CallSession) -> None:
        with self._lock:
            self._calls.pop(old_call_id, None)
            self._calls[session.call_id] = session

    def remove(self, call_id: str) -> None:
        with self._lock:
            self._calls.pop(call_id, None)

    def get(self, call_id: str) -> CallSession | None:
        with self._lock:
            return self._calls.get(call_id)

    def active_call_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._calls)
