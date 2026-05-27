from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .providers import (
    STT_OPENAI_COMPATIBLE_PROVIDERS,
    SUPPORTED_STT_PROVIDERS,
    SUPPORTED_TTS_PROVIDERS,
    TTS_OPENAI_COMPATIBLE_PROVIDERS,
    normalize_provider,
    unsupported_provider_message,
)

if TYPE_CHECKING:
    from .config import Settings


ProviderFactory = Callable[["Settings"], Any]


@dataclass
class ProviderRegistry:
    stt_factories: dict[str, ProviderFactory] = field(default_factory=dict)
    tts_factories: dict[str, ProviderFactory] = field(default_factory=dict)

    def register_stt(self, provider: str, factory: ProviderFactory) -> None:
        self.stt_factories[normalize_provider(provider)] = factory

    def register_tts(self, provider: str, factory: ProviderFactory) -> None:
        self.tts_factories[normalize_provider(provider)] = factory

    def build_stt(self, settings: Settings):
        provider = normalize_provider(settings.stt_provider)
        factory = self.stt_factories.get(provider)
        if factory is not None:
            return factory(settings)
        if provider in SUPPORTED_STT_PROVIDERS:
            raise ValueError(
                unsupported_provider_message(
                    "STT",
                    provider,
                    SUPPORTED_STT_PROVIDERS,
                    "Use an OpenAI-compatible endpoint with VOICEBOT_STT_PROVIDER=openai-compatible, "
                    "VOICEBOT_STT_BASE_URL, VOICEBOT_STT_API_KEY, and VOICEBOT_STT_MODEL until a native adapter is added.",
                )
            )
        raise ValueError(f"Unknown STT provider: {settings.stt_provider}")

    def build_tts(self, settings: Settings):
        provider = normalize_provider(settings.tts_provider)
        factory = self.tts_factories.get(provider)
        if factory is not None:
            return factory(settings)
        if provider in SUPPORTED_TTS_PROVIDERS:
            raise ValueError(
                unsupported_provider_message(
                    "TTS",
                    provider,
                    SUPPORTED_TTS_PROVIDERS,
                    "Use an OpenAI-compatible endpoint with VOICEBOT_TTS_PROVIDER=openai-compatible, "
                    "VOICEBOT_TTS_BASE_URL, VOICEBOT_TTS_API_KEY, VOICEBOT_TTS_MODEL, and "
                    "VOICEBOT_OPENAI_TTS_VOICE until a native adapter is added.",
                )
            )
        raise ValueError(f"Unknown TTS provider: {settings.tts_provider}")


def default_provider_registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register_stt("whisper", _build_whisper_stt)
    for provider in STT_OPENAI_COMPATIBLE_PROVIDERS:
        registry.register_stt(provider, _build_openai_stt)
    registry.register_tts("supertonic", _build_supertonic_tts)
    for provider in TTS_OPENAI_COMPATIBLE_PROVIDERS:
        registry.register_tts(provider, _build_openai_tts)
    return registry


def _build_whisper_stt(settings: Settings):
    from .stt import WhisperSTTProvider

    return WhisperSTTProvider(settings)


def _build_openai_stt(settings: Settings):
    from .stt import OpenAISTTProvider

    return OpenAISTTProvider(settings)


def _build_supertonic_tts(settings: Settings):
    from .tts import SupertonicTTSProvider

    return SupertonicTTSProvider(settings.tts_voice, settings.language)


def _build_openai_tts(settings: Settings):
    from .tts import OpenAITTSProvider

    return OpenAITTSProvider(settings)
