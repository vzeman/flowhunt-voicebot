from __future__ import annotations

from abc import ABC, abstractmethod
import threading

import numpy as np
from supertonic import TTS

from .audio import CALL_SAMPLE_RATE, resample_audio


class TTSProvider(ABC):
    @abstractmethod
    def synthesize(self, text: str) -> tuple[np.ndarray, float]:
        raise NotImplementedError


class SupertonicTTSProvider(TTSProvider):
    def __init__(self, voice_name: str, language: str | None) -> None:
        print("Loading Supertonic TTS model.")
        self._tts = TTS(auto_download=True)
        self._voice_style = self._tts.get_voice_style(voice_name=voice_name)
        self._language = language
        self._lock = threading.Lock()

    def synthesize(self, text: str) -> tuple[np.ndarray, float]:
        kwargs = {"voice_style": self._voice_style}
        if self._language:
            kwargs["lang"] = self._language
        with self._lock:
            wav, duration = self._tts.synthesize(text, **kwargs)

        duration_seconds = float(np.asarray(duration).reshape(-1)[0])
        audio = np.asarray(wav, dtype=np.float32).squeeze()
        call_audio = resample_audio(audio, self._tts.sample_rate, CALL_SAMPLE_RATE)
        return call_audio, duration_seconds
