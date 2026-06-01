from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
import difflib
import json
import re
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

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
        if len(normalized_text) >= 12 and (
            normalized_prompt.startswith(normalized_text)
            or normalized_text.startswith(normalized_prompt[: min(len(normalized_prompt), len(normalized_text))])
        ):
            return True
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


class HttpBatchSTTProvider(STTProvider):
    def __init__(self, settings: Settings) -> None:
        self._provider = normalize_provider(settings.stt_provider)
        self._api_key = provider_api_key(self._provider, settings.stt_api_key, "")
        self._base_url = settings.stt_base_url.strip() or _default_batch_stt_base_url(self._provider)
        self._model = settings.stt_model.strip() or _default_batch_stt_model(self._provider)
        self._language = _stt_language_hint(settings.language)
        self._timeout = settings.stt_timeout_seconds
        self._min_chars = settings.stt_min_chars
        self._lock = threading.Lock()
        if not self._api_key:
            raise ValueError(f"API key is required when VOICEBOT_STT_PROVIDER={self._provider}")
        if self._provider not in {"deepgram", "assemblyai"}:
            raise ValueError(f"Unsupported HTTP batch STT provider: {self._provider}")
        print(f"Using {self._provider} STT model: {self._model}")

    def transcribe(self, call_audio: np.ndarray, sample_rate: int = CALL_SAMPLE_RATE) -> TranscriptionResult:
        audio = resample_audio(call_audio, sample_rate, STT_SAMPLE_RATE)
        with tempfile.NamedTemporaryFile(suffix=".wav") as tmp:
            wavfile.write(tmp.name, STT_SAMPLE_RATE, np.clip(audio, -1.0, 1.0))
            with open(tmp.name, "rb") as audio_file:
                wav_bytes = audio_file.read()
        with self._lock:
            if self._provider == "deepgram":
                return self._transcribe_deepgram(wav_bytes)
            return self._transcribe_assemblyai(wav_bytes)

    def _transcribe_deepgram(self, wav_bytes: bytes) -> TranscriptionResult:
        query = {
            "model": self._model,
            "smart_format": "true",
            "punctuate": "true",
        }
        if self._language:
            query["language"] = self._language
        url = f"{self._base_url.rstrip('/')}/v1/listen?{urllib.parse.urlencode(query)}"
        payload = _json_request(
            "POST",
            url,
            headers={
                "Authorization": f"Token {self._api_key}",
                "Content-Type": "audio/wav",
            },
            body=wav_bytes,
            timeout=self._timeout,
        )
        alternatives = (
            payload.get("results", {})
            .get("channels", [{}])[0]
            .get("alternatives", [{}])
        )
        alternative = alternatives[0] if alternatives else {}
        text = str(alternative.get("transcript", "") or "").strip()
        metadata = {
            "provider": self._provider,
            "model": self._model,
            "language": self._language,
            "confidence": alternative.get("confidence"),
            "duration": payload.get("metadata", {}).get("duration"),
            "request_id": payload.get("metadata", {}).get("request_id"),
        }
        return self._result(text, metadata)

    def _transcribe_assemblyai(self, wav_bytes: bytes) -> TranscriptionResult:
        base = self._base_url.rstrip("/")
        headers = {"Authorization": self._api_key}
        upload = _json_request(
            "POST",
            f"{base}/v2/upload",
            headers={**headers, "Content-Type": "application/octet-stream"},
            body=wav_bytes,
            timeout=self._timeout,
        )
        audio_url = str(upload.get("upload_url", "") or "")
        if not audio_url:
            raise RuntimeError("AssemblyAI upload response did not include upload_url")
        request_body: dict[str, Any] = {"audio_url": audio_url}
        if self._language:
            request_body["language_code"] = self._language
        if self._model:
            request_body["speech_model"] = self._model
        submitted = _json_request(
            "POST",
            f"{base}/v2/transcript",
            headers={**headers, "Content-Type": "application/json"},
            body=json.dumps(request_body).encode("utf-8"),
            timeout=self._timeout,
        )
        transcript_id = str(submitted.get("id", "") or "")
        if not transcript_id:
            raise RuntimeError("AssemblyAI transcript response did not include id")
        deadline = time.monotonic() + max(0.1, self._timeout)
        poll_payload = submitted
        while time.monotonic() < deadline:
            status = str(poll_payload.get("status", "") or "").lower()
            if status == "completed":
                text = str(poll_payload.get("text", "") or "").strip()
                metadata = {
                    "provider": self._provider,
                    "model": self._model,
                    "language": poll_payload.get("language_code") or self._language,
                    "duration": poll_payload.get("audio_duration"),
                    "request_id": transcript_id,
                    "status": status,
                }
                return self._result(text, metadata)
            if status == "error":
                raise RuntimeError(str(poll_payload.get("error", "AssemblyAI transcription failed")))
            time.sleep(min(0.5, max(0.05, deadline - time.monotonic())))
            poll_payload = _json_request(
                "GET",
                f"{base}/v2/transcript/{urllib.parse.quote(transcript_id)}",
                headers=headers,
                timeout=self._timeout,
            )
        raise TimeoutError(f"AssemblyAI transcription did not complete within {self._timeout:g}s")

    def _result(self, text: str, metadata: dict[str, Any]) -> TranscriptionResult:
        if len(text) < self._min_chars:
            return TranscriptionResult("", "empty_or_too_short", metadata)
        if re.search(r"<\|.*?\|>", text):
            return TranscriptionResult("", "whisper_token_artifact", metadata)
        return TranscriptionResult(text, None, metadata)


def _default_batch_stt_base_url(provider: str) -> str:
    if provider == "deepgram":
        return "https://api.deepgram.com"
    if provider == "assemblyai":
        return "https://api.assemblyai.com"
    return ""


def _default_batch_stt_model(provider: str) -> str:
    if provider == "deepgram":
        return "nova-3"
    if provider == "assemblyai":
        return "universal"
    return ""


def _json_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    body: bytes | None = None,
    timeout: float = 8.0,
) -> dict[str, Any]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from STT provider: {raw[:1000]}") from exc
    if not 200 <= status < 300:
        raise RuntimeError(f"HTTP {status} from STT provider: {raw[:1000]}")
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"STT provider returned invalid JSON: {raw[:1000]}") from exc
    if not isinstance(data, dict):
        return {"value": data}
    return data


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
