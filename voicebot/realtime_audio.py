from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

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

    def __post_init__(self) -> None:
        if self.sample_rate <= 0:
            raise ValueError("sample_rate must be greater than 0")
        if self.start_threshold < 0:
            raise ValueError("start_threshold must be greater than or equal to 0")
        if self.stop_threshold < 0:
            raise ValueError("stop_threshold must be greater than or equal to 0")
        if self.stop_threshold > self.start_threshold:
            raise ValueError("stop_threshold must be less than or equal to start_threshold")
        if self.vad_start_ms < 0:
            raise ValueError("vad_start_ms must be greater than or equal to 0")
        if self.silence_ms <= 0:
            raise ValueError("silence_ms must be greater than 0")
        if self.min_seconds < 0:
            raise ValueError("min_seconds must be greater than or equal to 0")
        if self.max_seconds <= 0:
            raise ValueError("max_seconds must be greater than 0")
        if self.max_seconds < self.min_seconds:
            raise ValueError("max_seconds must be greater than or equal to min_seconds")
        if self.barge_in_threshold < self.start_threshold:
            raise ValueError("barge_in_threshold must be greater than or equal to start_threshold")


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
    silence_ms: int = 0
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
            "silence_ms": self.silence_ms,
        }
        if session_id:
            data["session_id"] = session_id
        if turn_id is not None:
            data["turn_id"] = turn_id
        return data


@dataclass(frozen=True)
class VoiceActivity:
    level: float
    active: bool


@runtime_checkable
class VoiceActivityDetector(Protocol):
    def detect(self, samples: np.ndarray, *, threshold: float) -> VoiceActivity:
        ...


class RmsVoiceActivityDetector:
    def detect(self, samples: np.ndarray, *, threshold: float) -> VoiceActivity:
        level = rms(samples)
        return VoiceActivity(level=level, active=level >= threshold)


@dataclass(frozen=True)
class AudioChunkNormalizer:
    source_rate: int
    target_rate: int
    channels: int = 1

    def __post_init__(self) -> None:
        if self.source_rate <= 0:
            raise ValueError("source_rate must be greater than 0")
        if self.target_rate <= 0:
            raise ValueError("target_rate must be greater than 0")
        if self.channels <= 0:
            raise ValueError("channels must be greater than 0")

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


@dataclass(frozen=True)
class JitterBufferConfig:
    sample_rate: int
    frame_ms: int = 20
    target_delay_ms: int = 60
    max_delay_ms: int = 200

    def __post_init__(self) -> None:
        if self.sample_rate <= 0:
            raise ValueError("sample_rate must be greater than 0")
        if self.frame_ms <= 0:
            raise ValueError("frame_ms must be greater than 0")
        if self.target_delay_ms < 0:
            raise ValueError("target_delay_ms must be greater than or equal to 0")
        if self.max_delay_ms < self.frame_ms:
            raise ValueError("max_delay_ms must be greater than or equal to frame_ms")
        if self.max_delay_ms < self.target_delay_ms:
            raise ValueError("max_delay_ms must be greater than or equal to target_delay_ms")

    @property
    def frame_samples(self) -> int:
        return max(1, int(self.sample_rate * self.frame_ms / 1000))

    @property
    def target_delay_samples(self) -> int:
        return int(self.sample_rate * self.target_delay_ms / 1000)

    @property
    def max_delay_samples(self) -> int:
        return max(self.frame_samples, int(self.sample_rate * self.max_delay_ms / 1000))


@dataclass
class AudioJitterBuffer:
    config: JitterBufferConfig
    _samples: deque[float] = field(default_factory=deque)

    def push(self, block: np.ndarray) -> None:
        samples = np.asarray(block, dtype=np.float32).reshape(-1)
        if samples.size == 0:
            return
        self._samples.extend(float(sample) for sample in samples)
        self._trim()

    def ready(self) -> bool:
        return len(self._samples) >= self.config.target_delay_samples + self.config.frame_samples

    def pop(self) -> np.ndarray | None:
        if not self.ready():
            return None
        frame = [self._samples.popleft() for _ in range(self.config.frame_samples)]
        return np.asarray(frame, dtype=np.float32)

    def buffered_samples(self) -> int:
        return len(self._samples)

    def buffered_ms(self) -> int:
        return int(len(self._samples) / self.config.sample_rate * 1000)

    def clear(self) -> None:
        self._samples.clear()

    def _trim(self) -> None:
        overflow = len(self._samples) - self.config.max_delay_samples
        for _ in range(max(0, overflow)):
            self._samples.popleft()


@dataclass
class DebugAudioCapture:
    enabled: bool
    sample_rate: int
    max_seconds: float = 30.0
    _blocks: deque[np.ndarray] = field(default_factory=deque)
    _samples: int = 0

    def __post_init__(self) -> None:
        if self.sample_rate <= 0:
            raise ValueError("sample_rate must be greater than 0")
        if self.max_seconds < 0:
            raise ValueError("max_seconds must be greater than or equal to 0")

    def append(self, block: np.ndarray) -> None:
        if not self.enabled:
            return
        samples = np.asarray(block, dtype=np.float32).reshape(-1)
        if samples.size == 0:
            return
        self._blocks.append(samples.copy())
        self._samples += int(samples.size)
        self._trim()

    def audio(self) -> np.ndarray:
        if not self._blocks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(list(self._blocks)).astype(np.float32, copy=False)

    def clear(self) -> None:
        self._blocks.clear()
        self._samples = 0

    def summary(self) -> dict:
        return {
            "enabled": self.enabled,
            "sample_rate": self.sample_rate,
            "samples": self._samples,
            "duration_seconds": self._samples / self.sample_rate if self.sample_rate else 0.0,
        }

    def _trim(self) -> None:
        max_samples = int(max(0.0, self.max_seconds) * self.sample_rate)
        if max_samples <= 0:
            self.clear()
            return
        while self._samples > max_samples and self._blocks:
            removed = self._blocks.popleft()
            self._samples -= int(removed.size)


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
    def __init__(
        self,
        config: TurnDetectionConfig,
        state: TurnDetectorState | None = None,
        vad: VoiceActivityDetector | None = None,
    ) -> None:
        self.config = config
        self.state = state or TurnDetectorState()
        self.vad = vad or RmsVoiceActivityDetector()

    def process_block(
        self,
        block: np.ndarray,
        *,
        playback_active: bool = False,
        echo_suppressed: bool = False,
    ) -> TurnDetectionResult:
        samples = block.astype(np.float32, copy=False).reshape(-1)
        block_ms = int(len(samples) / self.config.sample_rate * 1000) if self.config.sample_rate else 0
        active_threshold = self.config.barge_in_threshold if playback_active else self.config.start_threshold
        activity = self.vad.detect(samples, threshold=active_threshold)
        level = activity.level

        if samples.size == 0:
            return TurnDetectionResult("silence", level, block_ms)

        if not self.state.is_recording:
            if echo_suppressed or self.should_ignore_for_playback(activity, playback_active):
                self.state.reset_pending()
                return TurnDetectionResult("ignored", level, block_ms)
            if not activity.active:
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
        stop_activity = self.vad.detect(samples, threshold=self.config.stop_threshold)
        if not stop_activity.active:
            self.state.silence_ms += block_ms
        else:
            self.state.silence_ms = 0

        max_ms = int(self.config.max_seconds * 1000)
        if self.state.silence_ms < self.config.silence_ms and self.state.speech_ms < max_ms:
            return TurnDetectionResult("speech_continues", level, block_ms)

        audio = np.concatenate(self.state.collected) if self.state.collected else np.zeros(0, dtype=np.float32)
        duration = len(audio) / self.config.sample_rate if self.config.sample_rate else 0.0
        silence_ms = self.state.silence_ms
        self.state.reset_recording()
        if duration < self.config.min_seconds:
            return TurnDetectionResult(
                "speech_too_short",
                level,
                block_ms,
                finished=True,
                duration=duration,
                silence_ms=silence_ms,
                audio=audio,
            )
        return TurnDetectionResult(
            "speech_finished",
            level,
            block_ms,
            finished=True,
            duration=duration,
            silence_ms=silence_ms,
            audio=audio,
        )

    def should_ignore_for_playback(self, activity: VoiceActivity, playback_active: bool) -> bool:
        return playback_active and not activity.active
