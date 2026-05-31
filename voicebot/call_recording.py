from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO
import threading
import time
from typing import Any

import numpy as np
from scipy.io import wavfile

from .audio import rms, resample_audio

MAX_SEGMENT_MERGE_GAP_SECONDS = 0.12


@dataclass
class RecordingSegment:
    source: str
    start_seconds: float
    end_seconds: float
    sample_rate: int
    samples: np.ndarray
    metadata: dict[str, Any] = field(default_factory=dict)

    def public_metadata(self, playback_start_seconds: float) -> dict[str, Any]:
        duration = len(self.samples) / self.sample_rate if self.sample_rate else 0.0
        return {
            "source": self.source,
            "start_seconds": round(self.start_seconds, 3),
            "end_seconds": round(self.end_seconds, 3),
            "duration_seconds": round(duration, 3),
            "playback_start_seconds": round(playback_start_seconds, 3),
            "sample_rate": self.sample_rate,
            "samples": int(len(self.samples)),
            "metadata": self.metadata,
        }


class SpeechOnlyCallRecorder:
    def __init__(
        self,
        call_id: str,
        artifact_store: Any | None = None,
        silence_threshold: float = 0.003,
    ) -> None:
        self.call_id = call_id
        self.artifact_store = artifact_store
        self.silence_threshold = max(0.0, float(silence_threshold))
        self._started_at = time.monotonic()
        self._segments: list[RecordingSegment] = []
        self._finalized_metadata: dict[str, Any] | None = None
        self._lock = threading.Lock()

    def update_call_id(self, call_id: str) -> None:
        with self._lock:
            self.call_id = call_id

    def append_speech(
        self,
        source: str,
        audio: np.ndarray,
        sample_rate: int,
        *,
        end_monotonic: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        samples = np.asarray(audio, dtype=np.float32).reshape(-1)
        samples = strip_silence(samples, self.silence_threshold)
        if samples.size == 0 or rms(samples) < self.silence_threshold:
            return
        end = end_monotonic if end_monotonic is not None else time.monotonic()
        duration = len(samples) / sample_rate
        relative_end = max(0.0, end - self._started_at)
        segment = RecordingSegment(
            source=source,
            start_seconds=max(0.0, relative_end - duration),
            end_seconds=relative_end,
            sample_rate=sample_rate,
            samples=samples,
            metadata=dict(metadata or {}),
        )
        with self._lock:
            self._segments.append(segment)
            self._finalized_metadata = None

    def finalize(self) -> dict[str, Any] | None:
        with self._lock:
            if self._finalized_metadata is not None:
                return dict(self._finalized_metadata)
            if not self._segments or self.artifact_store is None:
                return None
            segments = coalesce_segments(sorted(self._segments, key=lambda item: item.start_seconds))
            sample_rate = segments[0].sample_rate
            playback_segments = [
                resample_audio(segment.samples, segment.sample_rate, sample_rate) for segment in segments
            ]
            audio = np.concatenate(playback_segments).astype(np.float32, copy=False)
            playback_position = 0.0
            segment_metadata: list[dict[str, Any]] = []
            for segment, playback_audio in zip(segments, playback_segments):
                public = segment.public_metadata(playback_position)
                public["playback_sample_rate"] = sample_rate
                public["playback_samples"] = int(len(playback_audio))
                segment_metadata.append(public)
                playback_position += len(playback_audio) / sample_rate
            artifact_id = recording_artifact_id(self.call_id)
            metadata = {
                "call_id": self.call_id,
                "kind": "speech_only_call_recording",
                "sample_rate": sample_rate,
                "segments": segment_metadata,
                "segment_count": len(segment_metadata),
                "duration_seconds": round(len(audio) / segments[0].sample_rate, 3),
                "original_voice_span_seconds": round(
                    max(segment.end_seconds for segment in segments) - min(segment.start_seconds for segment in segments),
                    3,
                ),
                "silence_removed": True,
            }
            self.artifact_store.put(artifact_id, wav_bytes(audio, segments[0].sample_rate), metadata)
            self._finalized_metadata = metadata
            return dict(metadata)


def strip_silence(audio: np.ndarray, threshold: float) -> np.ndarray:
    samples = np.asarray(audio, dtype=np.float32).reshape(-1)
    if samples.size == 0:
        return samples
    voiced = np.flatnonzero(np.abs(samples) >= threshold)
    if voiced.size == 0:
        return np.array([], dtype=np.float32)
    return samples[int(voiced[0]) : int(voiced[-1]) + 1]


def coalesce_segments(segments: list[RecordingSegment]) -> list[RecordingSegment]:
    if not segments:
        return []
    coalesced: list[RecordingSegment] = [segments[0]]
    for segment in segments[1:]:
        previous = coalesced[-1]
        if can_merge_segments(previous, segment):
            previous.samples = np.concatenate([previous.samples, segment.samples]).astype(np.float32, copy=False)
            previous.end_seconds = max(previous.end_seconds, segment.end_seconds)
            continue
        coalesced.append(segment)
    return coalesced


def can_merge_segments(left: RecordingSegment, right: RecordingSegment) -> bool:
    return (
        left.source == right.source
        and left.sample_rate == right.sample_rate
        and left.metadata == right.metadata
        and 0 <= right.start_seconds - left.end_seconds <= MAX_SEGMENT_MERGE_GAP_SECONDS
    )


def wav_bytes(audio: np.ndarray, sample_rate: int) -> bytes:
    buffer = BytesIO()
    wavfile.write(buffer, sample_rate, np.clip(audio, -1.0, 1.0).astype(np.float32, copy=False))
    return buffer.getvalue()


def recording_artifact_id(call_id: str) -> str:
    return f"{call_id}.speech.wav"
