from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from .audio import rms
from .audio import resample_audio


TurnDecision = Literal[
    "ignored",
    "silence",
    "pending_start",
    "speech_started",
    "speech_continues",
    "speech_finished",
    "speech_too_short",
]


@dataclass(frozen=True)
class TurnDetectionConfig:
    sample_rate: int
    start_threshold: float
    stop_threshold: float
    vad_start_ms: int
    silence_ms: int
    min_seconds: float
    max_seconds: float
    barge_in_threshold: float


def turn_detection_config_from_settings(settings, sample_rate: int) -> TurnDetectionConfig:
    return TurnDetectionConfig(
        sample_rate=sample_rate,
        start_threshold=settings.start_threshold,
        stop_threshold=settings.stop_threshold,
        vad_start_ms=settings.vad_start_ms,
        silence_ms=settings.silence_ms,
        min_seconds=settings.min_seconds,
        max_seconds=settings.max_seconds,
        barge_in_threshold=settings.barge_in_threshold,
    )


@dataclass(frozen=True)
class TurnDetectionResult:
    decision: TurnDecision
    level: float
    block_ms: int
    started: bool = False
    finished: bool = False
    interrupt_playback: bool = False
    duration: float = 0.0
    audio: np.ndarray | None = None

    def metric_data(self, *, session_id: str = "", turn_id: int | None = None) -> dict:
        data = {
            "decision": self.decision,
            "level": self.level,
            "block_ms": self.block_ms,
            "started": self.started,
            "finished": self.finished,
            "interrupt_playback": self.interrupt_playback,
            "duration": self.duration,
        }
        if session_id:
            data["session_id"] = session_id
        if turn_id is not None:
            data["turn_id"] = turn_id
        return data


@dataclass(frozen=True)
class AudioChunkNormalizer:
    source_rate: int
    target_rate: int
    channels: int = 1

    def normalize(self, block: np.ndarray) -> np.ndarray:
        samples = np.asarray(block)
        if samples.ndim > 1:
            samples = samples.mean(axis=0 if samples.shape[0] == self.channels else -1)
        if samples.dtype.kind in {"i", "u"}:
            samples = samples.astype(np.float32) / float(np.iinfo(samples.dtype).max)
        else:
            samples = samples.astype(np.float32, copy=False)
            if np.max(np.abs(samples), initial=0.0) > 1.0:
                samples = samples / 32768.0
        return resample_audio(samples.reshape(-1), self.source_rate, self.target_rate)


@dataclass
class TurnDetectorState:
    is_recording: bool = False
    collected: list[np.ndarray] = field(default_factory=list)
    pending_start: deque[np.ndarray] = field(default_factory=deque)
    pending_start_ms: int = 0
    silence_ms: int = 0
    speech_ms: int = 0

    def reset_pending(self) -> None:
        self.pending_start.clear()
        self.pending_start_ms = 0

    def reset_recording(self) -> None:
        self.is_recording = False
        self.collected = []
        self.silence_ms = 0
        self.speech_ms = 0


class TurnDetector:
    def __init__(self, config: TurnDetectionConfig, state: TurnDetectorState | None = None) -> None:
        self.config = config
        self.state = state or TurnDetectorState()

    def process_block(
        self,
        block: np.ndarray,
        *,
        playback_active: bool = False,
        echo_suppressed: bool = False,
    ) -> TurnDetectionResult:
        samples = block.astype(np.float32, copy=False).reshape(-1)
        block_ms = int(len(samples) / self.config.sample_rate * 1000) if self.config.sample_rate else 0
        level = rms(samples)

        if samples.size == 0:
            return TurnDetectionResult("silence", level, block_ms)

        if not self.state.is_recording:
            if echo_suppressed or self.should_ignore_for_playback(level, playback_active):
                self.state.reset_pending()
                return TurnDetectionResult("ignored", level, block_ms)
            if level < self.config.start_threshold:
                self.state.reset_pending()
                return TurnDetectionResult("silence", level, block_ms)

            self.state.pending_start.append(samples)
            self.state.pending_start_ms += block_ms
            if self.state.pending_start_ms < self.config.vad_start_ms:
                return TurnDetectionResult("pending_start", level, block_ms)

            self.state.is_recording = True
            self.state.collected = list(self.state.pending_start)
            self.state.reset_pending()
            self.state.silence_ms = 0
            self.state.speech_ms = sum(int(len(item) / self.config.sample_rate * 1000) for item in self.state.collected)
            return TurnDetectionResult(
                "speech_started",
                level,
                block_ms,
                started=True,
                interrupt_playback=playback_active,
            )

        self.state.collected.append(samples)
        self.state.speech_ms += block_ms
        if level < self.config.stop_threshold:
            self.state.silence_ms += block_ms
        else:
            self.state.silence_ms = 0

        max_ms = int(self.config.max_seconds * 1000)
        if self.state.silence_ms < self.config.silence_ms and self.state.speech_ms < max_ms:
            return TurnDetectionResult("speech_continues", level, block_ms)

        audio = np.concatenate(self.state.collected) if self.state.collected else np.zeros(0, dtype=np.float32)
        duration = len(audio) / self.config.sample_rate if self.config.sample_rate else 0.0
        self.state.reset_recording()
        if duration < self.config.min_seconds:
            return TurnDetectionResult("speech_too_short", level, block_ms, finished=True, duration=duration, audio=audio)
        return TurnDetectionResult("speech_finished", level, block_ms, finished=True, duration=duration, audio=audio)

    def should_ignore_for_playback(self, level: float, playback_active: bool) -> bool:
        return playback_active and level < self.config.barge_in_threshold
