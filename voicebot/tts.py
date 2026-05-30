from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
import hashlib
from io import BytesIO
import json
from pathlib import Path
import threading
from typing import Protocol

import numpy as np

try:
    from openai import OpenAI
except ModuleNotFoundError:
    OpenAI = None

try:
    import soundfile as sf
except ModuleNotFoundError:
    sf = None

try:
    from supertonic import TTS
except ModuleNotFoundError:
    TTS = None

from .audio import CALL_SAMPLE_RATE, resample_audio
from .config import Settings
from .providers import normalize_provider, provider_api_key, provider_base_url
from .storage import ArtifactStoreProtocol, FilesystemArtifactStore

OPENAI_TTS_PCM_SAMPLE_RATE = 24_000


class TTSProvider(ABC):
    @abstractmethod
    def synthesize(self, text: str) -> tuple[np.ndarray, float]:
        raise NotImplementedError

    def synthesize_stream(self, text: str) -> Iterable[tuple[np.ndarray, float]]:
        yield self.synthesize(text)


@dataclass(frozen=True)
class TTSCacheConfig:
    provider: str
    model: str
    voice: str
    language: str | None
    sample_rate: int = CALL_SAMPLE_RATE


class _ArtifactReaderWriter(Protocol):
    def get(self, artifact_id: str) -> bytes | None:
        ...

    def put(self, artifact_id: str, data: bytes, metadata: dict | None = None):
        ...


class CachedTTSProvider(TTSProvider):
    def __init__(
        self,
        inner: TTSProvider,
        cache_dir: str,
        config: TTSCacheConfig,
        artifact_store: ArtifactStoreProtocol | None = None,
    ) -> None:
        self._inner = inner
        self._cache_dir = Path(cache_dir)
        self._artifact_store: _ArtifactReaderWriter = artifact_store or FilesystemArtifactStore(cache_dir)
        self._config = config
        self._lock = threading.Lock()
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def synthesize(self, text: str) -> tuple[np.ndarray, float]:
        key = self._cache_key(text)
        artifact_id = f"{key}.npz"
        cached = self._read(artifact_id)
        if cached is not None:
            return cached

        audio, duration = self._inner.synthesize(text)
        self._write(artifact_id, audio, duration)
        return audio, duration

    def _cache_key(self, text: str) -> str:
        payload = {
            "version": 1,
            "config": {
                "provider": self._config.provider,
                "model": self._config.model,
                "voice": self._config.voice,
                "language": self._config.language,
                "sample_rate": self._config.sample_rate,
            },
            "text": text,
        }
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(serialized).hexdigest()

    def _read(self, artifact_id: str) -> tuple[np.ndarray, float] | None:
        try:
            with self._lock:
                cached_bytes = self._artifact_store.get(artifact_id)
                if cached_bytes is None:
                    return None
                with np.load(BytesIO(cached_bytes)) as cached:
                    audio = np.asarray(cached["audio"], dtype=np.float32)
                    duration = float(np.asarray(cached["duration"]).reshape(-1)[0])
        except (OSError, KeyError, ValueError):
            return None
        return audio, duration

    def _write(self, artifact_id: str, audio: np.ndarray, duration: float) -> None:
        with self._lock:
            buffer = BytesIO()
            np.savez_compressed(buffer, audio=audio.astype(np.float32, copy=False), duration=np.asarray([duration]))
            self._artifact_store.put(
                artifact_id,
                buffer.getvalue(),
                {
                    "kind": "tts_cache",
                    "provider": self._config.provider,
                    "model": self._config.model,
                    "voice": self._config.voice,
                    "language": self._config.language,
                    "sample_rate": self._config.sample_rate,
                },
            )


class SupertonicTTSProvider(TTSProvider):
    def __init__(self, voice_name: str, language: str | None) -> None:
        if TTS is None:
            raise RuntimeError("The supertonic package is required when using Supertonic TTS")
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
        if OpenAI is None:
            raise RuntimeError("The openai package is required when using OpenAI-compatible TTS")
        if sf is None:
            raise RuntimeError("The soundfile package is required when using OpenAI-compatible TTS")
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

    def synthesize_stream(self, text: str) -> Iterable[tuple[np.ndarray, float]]:
        with self._lock:
            with self._client.audio.speech.with_streaming_response.create(
                model=self._model,
                voice=self._voice,
                input=text,
                response_format="pcm",
            ) as response:
                pending = b""
                for chunk in response.iter_bytes(chunk_size=4096):
                    pending += chunk
                    even_length = len(pending) - (len(pending) % 2)
                    if even_length <= 0:
                        continue
                    pcm = pending[:even_length]
                    pending = pending[even_length:]
                    audio = pcm16le_bytes_to_float32(pcm)
                    if audio.size == 0:
                        continue
                    call_audio = resample_audio(audio, OPENAI_TTS_PCM_SAMPLE_RATE, CALL_SAMPLE_RATE)
                    if call_audio.size:
                        yield call_audio, audio.size / float(OPENAI_TTS_PCM_SAMPLE_RATE)


def pcm16le_bytes_to_float32(content: bytes) -> np.ndarray:
    even_length = len(content) - (len(content) % 2)
    if even_length <= 0:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(content[:even_length], dtype="<i2").astype(np.float32) / 32768.0
