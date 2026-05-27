from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from io import BytesIO
import threading

import numpy as np
from openai import OpenAI
import soundfile as sf
from supertonic import TTS

from .audio import CALL_SAMPLE_RATE, resample_audio
from .config import Settings
from .providers import normalize_provider, provider_api_key, provider_base_url


class TTSProvider(ABC):
    @abstractmethod
    def synthesize(self, text: str) -> tuple[np.ndarray, float]:
        raise NotImplementedError

    def synthesize_stream(self, text: str) -> Iterable[tuple[np.ndarray, float]]:
        yield self.synthesize(text)


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


class OpenAITTSProvider(TTSProvider):
    def __init__(self, settings: Settings) -> None:
        provider = normalize_provider(settings.tts_provider)
        api_key = provider_api_key(provider, settings.tts_api_key, settings.openai_api_key)
        base_url = provider_base_url(provider, settings.tts_base_url, settings.openai_base_url)
        model = settings.tts_model or settings.openai_tts_model
        if not api_key:
            raise ValueError(f"API key is required when VOICEBOT_TTS_PROVIDER={provider}")
        print(f"Using {provider} TTS model: {model} voice={settings.openai_tts_voice}")
        client_kwargs = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        self._client = OpenAI(**client_kwargs)
        self._provider = provider
        self._model = model
        self._voice = settings.openai_tts_voice
        self._lock = threading.Lock()

    def synthesize(self, text: str) -> tuple[np.ndarray, float]:
        with self._lock:
            response = self._client.audio.speech.create(
                model=self._model,
                voice=self._voice,
                input=text,
                response_format="wav",
            )
            content = response.read()

        wav, sample_rate = sf.read(BytesIO(content), dtype="float32")
        audio = np.asarray(wav, dtype=np.float32)
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        call_audio = resample_audio(audio, sample_rate, CALL_SAMPLE_RATE)
        return call_audio, len(audio) / float(sample_rate)
