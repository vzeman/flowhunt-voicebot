from __future__ import annotations

from abc import ABC, abstractmethod
import tempfile
import threading

import numpy as np
from scipy.io import wavfile
import whisper

from .audio import CALL_SAMPLE_RATE, STT_SAMPLE_RATE, resample_audio


class STTProvider(ABC):
    @abstractmethod
    def transcribe(self, call_audio: np.ndarray) -> str:
        raise NotImplementedError


class WhisperSTTProvider(STTProvider):
    def __init__(self, model_name: str, language: str | None) -> None:
        print(f"Loading Whisper STT model: {model_name}")
        self._model = whisper.load_model(model_name)
        self._language = language
        self._lock = threading.Lock()

    def transcribe(self, call_audio: np.ndarray) -> str:
        audio = resample_audio(call_audio, CALL_SAMPLE_RATE, STT_SAMPLE_RATE)
        with tempfile.NamedTemporaryFile(suffix=".wav") as tmp:
            wavfile.write(tmp.name, STT_SAMPLE_RATE, np.clip(audio, -1.0, 1.0))
            with self._lock:
                result = self._model.transcribe(tmp.name, language=self._language, fp16=False)
        return str(result.get("text", "")).strip()
