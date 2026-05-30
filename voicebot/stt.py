from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
import difflib
import re
import tempfile
import threading
import unicodedata

import numpy as np
from scipy.io import wavfile

try:
    import whisper
except ModuleNotFoundError:
    whisper = None

from .audio import CALL_SAMPLE_RATE, STT_SAMPLE_RATE, resample_audio
from .config import Settings
from .language import normalize_language_hint
from .providers import normalize_provider, provider_api_key, provider_base_url

try:
    from openai import OpenAI
except ModuleNotFoundError:
    OpenAI = None


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    reason: str | None = None
    metadata: dict | None = None
    is_final: bool = True


class STTProvider(ABC):
    @abstractmethod
    def transcribe(self, call_audio: np.ndarray, sample_rate: int = CALL_SAMPLE_RATE) -> TranscriptionResult:
        raise NotImplementedError

    def transcribe_stream(self, call_audio: np.ndarray, sample_rate: int = CALL_SAMPLE_RATE) -> Iterable[TranscriptionResult]:
        yield self.transcribe(call_audio, sample_rate)


class WhisperSTTProvider(STTProvider):
    def __init__(self, settings: Settings) -> None:
        if whisper is None:
            raise RuntimeError("The whisper package is required when using local Whisper STT")
        print(f"Loading Whisper STT model: {settings.whisper_model}")
        self._model = whisper.load_model(settings.whisper_model)
        self._language = _stt_language_hint(settings.language)
        self._no_speech_threshold = settings.stt_no_speech_threshold
        self._logprob_threshold = settings.stt_logprob_threshold
        self._min_chars = settings.stt_min_chars
        self._lock = threading.Lock()

    def transcribe(self, call_audio: np.ndarray, sample_rate: int = CALL_SAMPLE_RATE) -> TranscriptionResult:
        audio = resample_audio(call_audio, sample_rate, STT_SAMPLE_RATE)
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


class OpenAISTTProvider(STTProvider):
    def __init__(self, settings: Settings) -> None:
        if OpenAI is None:
            raise RuntimeError("The openai package is required when using OpenAI-compatible STT")
        provider = normalize_provider(settings.stt_provider)
        api_key = provider_api_key(provider, settings.stt_api_key, settings.openai_api_key)
        base_url = provider_base_url(provider, settings.stt_base_url, settings.openai_base_url)
        model = settings.stt_model or settings.openai_stt_model
        if not api_key:
            raise ValueError(f"API key is required when VOICEBOT_STT_PROVIDER={provider}")
        print(f"Using {provider} STT model: {model}")
        client_kwargs = {"api_key": api_key, "timeout": settings.stt_timeout_seconds}
        if base_url:
            client_kwargs["base_url"] = base_url
        self._client = OpenAI(**client_kwargs)
        self._provider = provider
        self._model = model
        self._language = _stt_language_hint(settings.language)
        self._prompt = settings.stt_prompt
        self._min_chars = settings.stt_min_chars
        self._lock = threading.Lock()

    def transcribe(self, call_audio: np.ndarray, sample_rate: int = CALL_SAMPLE_RATE) -> TranscriptionResult:
        audio = resample_audio(call_audio, sample_rate, STT_SAMPLE_RATE)
        return self._transcribe_audio(audio, use_prompt=bool(self._prompt))

    def _transcribe_audio(self, audio: np.ndarray, use_prompt: bool) -> TranscriptionResult:
        with tempfile.NamedTemporaryFile(suffix=".wav") as tmp:
            wavfile.write(tmp.name, STT_SAMPLE_RATE, np.clip(audio, -1.0, 1.0))
            with open(tmp.name, "rb") as audio_file:
                kwargs = {
                    "model": self._model,
                    "file": audio_file,
                    "response_format": self._response_format(),
                }
                if self._language:
                    kwargs["language"] = self._language
                if use_prompt and self._prompt:
                    kwargs["prompt"] = self._prompt
                with self._lock:
                    result = self._client.audio.transcriptions.create(**kwargs)

        text = str(getattr(result, "text", "") or "").strip()
        metadata = self._metadata(result)
        metadata["stt_prompt_used"] = use_prompt and bool(self._prompt)
        if len(text) < self._min_chars:
            return TranscriptionResult("", "empty_or_too_short", metadata)
        if re.search(r"<\|.*?\|>", text):
            return TranscriptionResult("", "whisper_token_artifact", metadata)
        if self._looks_like_prompt_echo(text):
            metadata["rejected_text_preview"] = text[:200]
            metadata["stt_prompt_configured"] = True
            if use_prompt:
                return self._transcribe_audio(audio, use_prompt=False)
            return TranscriptionResult("", "stt_prompt_echo", metadata)
        return TranscriptionResult(text, None, metadata)

    def _metadata(self, result) -> dict:
        segments = getattr(result, "segments", None) or []
        return {
            "provider": self._provider,
            "model": self._model,
            "language": getattr(result, "language", None),
            "duration": getattr(result, "duration", None),
            "segments": len(segments),
        }

    def _response_format(self) -> str:
        if self._model in {"gpt-4o-transcribe", "gpt-4o-mini-transcribe"}:
            return "json"
        return "verbose_json"

    def _looks_like_prompt_echo(self, text: str) -> bool:
        if not self._prompt:
            return False
        normalized_text = _normalize_for_similarity(text)
        normalized_prompt = _normalize_for_similarity(self._prompt)
        if not normalized_text or not normalized_prompt:
            return False
        if difflib.SequenceMatcher(None, normalized_text, normalized_prompt).ratio() >= 0.70:
            return True

        prompt_terms = _keyword_set(normalized_prompt)
        text_terms = _keyword_set(normalized_text)
        overlap_ratio = len(prompt_terms & text_terms) / max(len(prompt_terms), 1)
        prompt_summary_markers = (
            "caller mentioned",
            "range of topics",
            "may have questions",
            "support ticket details",
            "technologies and services",
        )
        if overlap_ratio >= 0.25 and any(marker in normalized_text for marker in prompt_summary_markers):
            return True
        return False


def _average(values: list[float]) -> float | None:
    if not values:
        return None
    return float(sum(values) / len(values))


def _stt_language_hint(language: str | None) -> str | None:
    return normalize_language_hint(language)


def _normalize_for_similarity(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text.lower())).strip()


def _keyword_set(normalized_text: str) -> set[str]:
    stopwords = {
        "caller",
        "mention",
        "mentioned",
        "mentions",
        "details",
        "support",
        "ticket",
        "project",
        "projects",
    }
    return {word for word in normalized_text.split() if len(word) >= 4 and word not in stopwords}


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
