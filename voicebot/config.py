from __future__ import annotations

from dataclasses import dataclass
import os


def env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    api_host: str = os.getenv("VOICEBOT_API_HOST", "0.0.0.0")
    api_port: int = env_int("VOICEBOT_API_PORT", 8080)
    audiosocket_host: str = os.getenv("VOICEBOT_AUDIOSOCKET_HOST", "0.0.0.0")
    audiosocket_port: int = env_int("VOICEBOT_AUDIOSOCKET_PORT", 9019)

    stt_provider: str = os.getenv("VOICEBOT_STT_PROVIDER", "whisper")
    whisper_model: str = os.getenv("VOICEBOT_WHISPER_MODEL", "base")
    language: str | None = os.getenv("VOICEBOT_LANGUAGE") or None

    tts_provider: str = os.getenv("VOICEBOT_TTS_PROVIDER", "supertonic")
    tts_voice: str = os.getenv("VOICEBOT_TTS_VOICE", "M1")

    start_threshold: float = env_float("VOICEBOT_START_THRESHOLD", 0.018)
    stop_threshold: float = env_float("VOICEBOT_STOP_THRESHOLD", 0.010)
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
