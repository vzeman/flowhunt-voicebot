from __future__ import annotations

from .providers import (
    AGENT_CHAT_COMPATIBLE_PROVIDERS,
    STT_OPENAI_COMPATIBLE_PROVIDERS,
    SUPPORTED_AGENT_PROVIDERS,
    SUPPORTED_STT_PROVIDERS,
    SUPPORTED_TTS_PROVIDERS,
    TTS_OPENAI_COMPATIBLE_PROVIDERS,
)


def provider_catalog() -> dict[str, dict[str, list[str]]]:
    return {
        "stt": {
            "supported": sorted(SUPPORTED_STT_PROVIDERS),
            "native": ["whisper"],
            "openai_compatible": sorted(STT_OPENAI_COMPATIBLE_PROVIDERS),
        },
        "tts": {
            "supported": sorted(SUPPORTED_TTS_PROVIDERS),
            "native": ["supertonic"],
            "openai_compatible": sorted(TTS_OPENAI_COMPATIBLE_PROVIDERS),
        },
        "agent": {
            "supported": sorted(SUPPORTED_AGENT_PROVIDERS),
            "native": ["openai-responses"],
            "chat_compatible": sorted(AGENT_CHAT_COMPATIBLE_PROVIDERS),
        },
    }
