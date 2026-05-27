from __future__ import annotations

from dataclasses import dataclass
from dataclasses import fields
import json
import os
from typing import Any


def env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_json_list(name: str, default: list[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    value = os.getenv(name)
    if value is None or not value.strip():
        return tuple(default)
    parsed = json.loads(value)
    if not isinstance(parsed, list):
        raise ValueError(f"{name} must be a JSON list")
    if not all(isinstance(item, dict) for item in parsed):
        raise ValueError(f"{name} must contain JSON objects")
    return tuple(parsed)


@dataclass(frozen=True)
class Settings:
    api_host: str = os.getenv("VOICEBOT_API_HOST", "0.0.0.0")
    api_port: int = env_int("VOICEBOT_API_PORT", 8080)
    audiosocket_host: str = os.getenv("VOICEBOT_AUDIOSOCKET_HOST", "0.0.0.0")
    audiosocket_port: int = env_int("VOICEBOT_AUDIOSOCKET_PORT", 9019)

    stt_provider: str = os.getenv("VOICEBOT_STT_PROVIDER", "whisper")
    stt_api_key: str = os.getenv("VOICEBOT_STT_API_KEY", "")
    stt_base_url: str = os.getenv("VOICEBOT_STT_BASE_URL", "")
    stt_model: str = os.getenv("VOICEBOT_STT_MODEL", "")
    whisper_model: str = os.getenv("VOICEBOT_WHISPER_MODEL", "base")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_base_url: str = os.getenv("VOICEBOT_OPENAI_BASE_URL", os.getenv("OPENAI_BASE_URL", ""))
    openai_stt_model: str = os.getenv("VOICEBOT_OPENAI_STT_MODEL", "whisper-1")
    language: str | None = os.getenv("VOICEBOT_LANGUAGE") or None
    stt_no_speech_threshold: float = env_float("VOICEBOT_STT_NO_SPEECH_THRESHOLD", 0.60)
    stt_logprob_threshold: float = env_float("VOICEBOT_STT_LOGPROB_THRESHOLD", -1.0)
    stt_min_chars: int = env_int("VOICEBOT_STT_MIN_CHARS", 2)

    tts_provider: str = os.getenv("VOICEBOT_TTS_PROVIDER", "supertonic")
    tts_api_key: str = os.getenv("VOICEBOT_TTS_API_KEY", "")
    tts_base_url: str = os.getenv("VOICEBOT_TTS_BASE_URL", "")
    tts_model: str = os.getenv("VOICEBOT_TTS_MODEL", "")
    tts_voice: str = os.getenv("VOICEBOT_TTS_VOICE", "M1")
    openai_tts_model: str = os.getenv("VOICEBOT_OPENAI_TTS_MODEL", "gpt-4o-mini-tts")
    openai_tts_voice: str = os.getenv("VOICEBOT_OPENAI_TTS_VOICE", "alloy")

    start_threshold: float = env_float("VOICEBOT_START_THRESHOLD", 0.030)
    stop_threshold: float = env_float("VOICEBOT_STOP_THRESHOLD", 0.012)
    vad_start_ms: int = env_int("VOICEBOT_VAD_START_MS", 120)
    barge_in_threshold: float = env_float("VOICEBOT_BARGE_IN_THRESHOLD", 0.30)
    echo_tail_ms: int = env_int("VOICEBOT_ECHO_TAIL_MS", 800)
    silence_ms: int = env_int("VOICEBOT_SILENCE_MS", 900)
    min_seconds: float = env_float("VOICEBOT_MIN_SECONDS", 0.5)
    max_seconds: float = env_float("VOICEBOT_MAX_SECONDS", 20.0)
    packet_ms: int = env_int("VOICEBOT_PACKET_MS", 20)

    max_context_events: int = env_int("VOICEBOT_MAX_CONTEXT_EVENTS", 80)
    transcript_dir: str = os.getenv("VOICEBOT_TRANSCRIPT_DIR", "/data/transcripts")
    greet_on_connect: bool = env_bool("VOICEBOT_GREET_ON_CONNECT", True)
    connect_greeting_prompt: str = os.getenv(
        "VOICEBOT_CONNECT_GREETING_PROMPT",
        "The call has connected. Greet the caller and ask how you can help.",
    )

    ami_host: str = os.getenv("VOICEBOT_AMI_HOST", "asterisk")
    ami_port: int = env_int("VOICEBOT_AMI_PORT", 5038)
    ami_username: str = os.getenv("VOICEBOT_AMI_USERNAME", "voicebot")
    ami_password: str = os.getenv("VOICEBOT_AMI_PASSWORD", "")

    stt_pipeline: tuple[dict[str, Any], ...] = env_json_list(
        "VOICEBOT_STT_PIPELINE",
        [{"name": "stt"}, {"name": "agent-request"}],
    )
    tts_pipeline: tuple[dict[str, Any], ...] = env_json_list(
        "VOICEBOT_TTS_PIPELINE",
        [{"name": "tts"}],
    )


SENSITIVE_FIELD_MARKERS = ("api_key", "password", "secret", "token")


def redacted_settings(settings: Settings) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in fields(settings):
        value = getattr(settings, field.name)
        if is_sensitive_field(field.name):
            result[field.name] = {
                "configured": bool(value),
                "redacted": True,
            }
        else:
            result[field.name] = json_safe_value(value)
    return result


def is_sensitive_field(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in SENSITIVE_FIELD_MARKERS)


def json_safe_value(value: Any) -> Any:
    if isinstance(value, tuple):
        return [json_safe_value(item) for item in value]
    if isinstance(value, list):
        return [json_safe_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_safe_value(item) for key, item in value.items()}
    return value
