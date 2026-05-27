from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import re
import tempfile
import threading
import unicodedata

import numpy as np
from scipy.io import wavfile
import whisper

from .audio import CALL_SAMPLE_RATE, STT_SAMPLE_RATE, resample_audio
from .config import Settings


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    reason: str | None = None
    metadata: dict | None = None


class STTProvider(ABC):
    @abstractmethod
    def transcribe(self, call_audio: np.ndarray) -> TranscriptionResult:
        raise NotImplementedError


class WhisperSTTProvider(STTProvider):
    def __init__(self, settings: Settings) -> None:
        print(f"Loading Whisper STT model: {settings.whisper_model}")
        self._model = whisper.load_model(settings.whisper_model)
        self._language = settings.language
        self._no_speech_threshold = settings.stt_no_speech_threshold
        self._logprob_threshold = settings.stt_logprob_threshold
        self._min_chars = settings.stt_min_chars
        self._lock = threading.Lock()

    def transcribe(self, call_audio: np.ndarray) -> TranscriptionResult:
        audio = resample_audio(call_audio, CALL_SAMPLE_RATE, STT_SAMPLE_RATE)
        with tempfile.NamedTemporaryFile(suffix=".wav") as tmp:
            wavfile.write(tmp.name, STT_SAMPLE_RATE, np.clip(audio, -1.0, 1.0))
            with self._lock:
                result = self._model.transcribe(
                    tmp.name,
                    language=self._language,
                    fp16=False,
                    temperature=0,
                    condition_on_previous_text=False,
                    no_speech_threshold=self._no_speech_threshold,
                    logprob_threshold=self._logprob_threshold,
                    compression_ratio_threshold=2.4,
                )
        text = str(result.get("text", "")).strip()
        metadata = self._metadata(result)

        rejection = self._rejection_reason(text, metadata)
        if rejection:
            return TranscriptionResult("", rejection, metadata)
        return TranscriptionResult(text, None, metadata)

    def _metadata(self, result: dict) -> dict:
        segments = result.get("segments") or []
        no_speech_values = [segment.get("no_speech_prob") for segment in segments if segment.get("no_speech_prob") is not None]
        logprob_values = [segment.get("avg_logprob") for segment in segments if segment.get("avg_logprob") is not None]
        return {
            "language": result.get("language"),
            "segments": len(segments),
            "avg_no_speech_prob": _average(no_speech_values),
            "avg_logprob": _average(logprob_values),
        }

    def _rejection_reason(self, text: str, metadata: dict) -> str | None:
        if len(text) < self._min_chars:
            return "empty_or_too_short"
        if re.search(r"<\|.*?\|>", text):
            return "whisper_token_artifact"
        avg_no_speech = metadata.get("avg_no_speech_prob")
        if avg_no_speech is not None and avg_no_speech >= self._no_speech_threshold:
            return "high_no_speech_probability"
        avg_logprob = metadata.get("avg_logprob")
        if avg_logprob is not None and avg_logprob < self._logprob_threshold:
            return "low_average_logprob"
        if _non_latin_letter_ratio(text) > 0.30:
            return "unexpected_non_latin_text"
        return None


def _average(values: list[float]) -> float | None:
    if not values:
        return None
    return float(sum(values) / len(values))


def _non_latin_letter_ratio(text: str) -> float:
    letters = [char for char in text if unicodedata.category(char).startswith("L")]
    if not letters:
        return 0.0
    non_latin = 0
    for char in letters:
        try:
            name = unicodedata.name(char)
        except ValueError:
            non_latin += 1
            continue
        if not name.startswith("LATIN "):
            non_latin += 1
    return non_latin / len(letters)
