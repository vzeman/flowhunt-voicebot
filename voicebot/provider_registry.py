from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .providers import (
    STT_OPENAI_COMPATIBLE_PROVIDERS,
    SUPPORTED_STT_PROVIDERS,
    SUPPORTED_TTS_PROVIDERS,
    TTS_OPENAI_COMPATIBLE_PROVIDERS,
    ProviderCapabilities,
    ProviderDescriptor,
    normalize_provider,
    unsupported_provider_message,
)
from .transports import CallRoute

if TYPE_CHECKING:
    from .config import Settings


ProviderFactory = Callable[["Settings"], Any]


@dataclass
class ProviderRegistry:
    stt_factories: dict[str, ProviderFactory] = field(default_factory=dict)
    tts_factories: dict[str, ProviderFactory] = field(default_factory=dict)
    stt_descriptors: dict[str, ProviderDescriptor] = field(default_factory=dict)
    tts_descriptors: dict[str, ProviderDescriptor] = field(default_factory=dict)
    stt_routes: dict[tuple[str | None, str | None], str] = field(default_factory=dict)
    tts_routes: dict[tuple[str | None, str | None], str] = field(default_factory=dict)

    def register_stt(
        self,
        provider: str,
        factory: ProviderFactory,
        descriptor: ProviderDescriptor | None = None,
    ) -> None:
        normalized = normalize_provider(provider)
        self.stt_factories[normalized] = factory
        self.stt_descriptors[normalized] = descriptor or _default_stt_descriptor(normalized)

    def register_tts(
        self,
        provider: str,
        factory: ProviderFactory,
        descriptor: ProviderDescriptor | None = None,
    ) -> None:
        normalized = normalize_provider(provider)
        self.tts_factories[normalized] = factory
        self.tts_descriptors[normalized] = descriptor or _default_tts_descriptor(normalized)

    def route_stt(self, workspace_id: str | None, voicebot_id: str | None, provider: str) -> None:
        self._route_provider(self.stt_routes, self.stt_factories, workspace_id, voicebot_id, provider, "STT")

    def route_tts(self, workspace_id: str | None, voicebot_id: str | None, provider: str) -> None:
        self._route_provider(self.tts_routes, self.tts_factories, workspace_id, voicebot_id, provider, "TTS")

    def build_stt(self, settings: Settings, route: CallRoute | None = None):
        provider = self.resolve_stt_provider(settings, route)
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
        raise ValueError(f"Unknown STT provider: {provider}")

    def build_tts(self, settings: Settings, route: CallRoute | None = None):
        provider = self.resolve_tts_provider(settings, route)
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
        raise ValueError(f"Unknown TTS provider: {provider}")

    def resolve_stt_provider(self, settings: Settings, route: CallRoute | None = None) -> str:
        return self._resolve_provider(self.stt_routes, route, settings.stt_provider)

    def resolve_tts_provider(self, settings: Settings, route: CallRoute | None = None) -> str:
        return self._resolve_provider(self.tts_routes, route, settings.tts_provider)

    def describe_stt(self, provider: str) -> ProviderDescriptor | None:
        return self.stt_descriptors.get(normalize_provider(provider))

    def describe_tts(self, provider: str) -> ProviderDescriptor | None:
        return self.tts_descriptors.get(normalize_provider(provider))

    def _resolve_provider(
        self,
        routes: dict[tuple[str | None, str | None], str],
        route: CallRoute | None,
        default_provider: str,
    ) -> str:
        if route is not None:
            keys = (
                (route.workspace_id, route.voicebot_id),
                (route.workspace_id, None),
                (None, route.voicebot_id),
            )
            for key in keys:
                provider = routes.get(key)
                if provider:
                    return provider
        return normalize_provider(default_provider)

    def _route_provider(
        self,
        routes: dict[tuple[str | None, str | None], str],
        factories: dict[str, ProviderFactory],
        workspace_id: str | None,
        voicebot_id: str | None,
        provider: str,
        family: str,
    ) -> None:
        normalized = normalize_provider(provider)
        if normalized not in factories:
            raise ValueError(f"Cannot route {family} provider '{normalized}' because no adapter is registered")
        routes[(workspace_id, voicebot_id)] = normalized


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
    from .tts import CachedTTSProvider, SupertonicTTSProvider, TTSCacheConfig

    provider = SupertonicTTSProvider(settings.tts_voice, settings.language)
    if not settings.tts_cache_enabled:
        return provider
    return CachedTTSProvider(
        provider,
        settings.tts_cache_dir,
        TTSCacheConfig(
            provider="supertonic",
            model="supertonic-3",
            voice=settings.tts_voice,
            language=settings.language,
        ),
    )


def _build_openai_tts(settings: Settings):
    from .tts import CachedTTSProvider, OpenAITTSProvider, TTSCacheConfig

    provider = OpenAITTSProvider(settings)
    if not settings.tts_cache_enabled:
        return provider
    normalized_provider = normalize_provider(settings.tts_provider)
    return CachedTTSProvider(
        provider,
        settings.tts_cache_dir,
        TTSCacheConfig(
            provider=normalized_provider,
            model=settings.tts_model or settings.openai_tts_model,
            voice=settings.openai_tts_voice,
            language=settings.language,
        ),
    )


def _default_stt_descriptor(provider: str) -> ProviderDescriptor:
    if provider == "whisper":
        return ProviderDescriptor(
            provider=provider,
            family="stt",
            adapter="native",
            capabilities=ProviderCapabilities(
                modalities=frozenset({"stt"}),
                languages=(),
                latency_profile="batch",
                interruption_support=True,
                usage_metadata=("duration", "language", "segments", "confidence"),
            ),
            models=("tiny", "base", "small", "medium", "large", "turbo"),
        )
    return ProviderDescriptor(
        provider=provider,
        family="stt",
        adapter="openai_compatible",
        capabilities=ProviderCapabilities(
            modalities=frozenset({"stt"}),
            languages=(),
            required_credentials=("api_key",),
            latency_profile="interactive",
            interruption_support=True,
            usage_metadata=("duration", "language", "segments"),
        ),
    )


def _default_tts_descriptor(provider: str) -> ProviderDescriptor:
    if provider == "supertonic":
        return ProviderDescriptor(
            provider=provider,
            family="tts",
            adapter="native",
            capabilities=ProviderCapabilities(
                modalities=frozenset({"tts"}),
                languages=(),
                latency_profile="batch",
                interruption_support=True,
                output_audio_format="pcm_f32_8000",
                usage_metadata=("duration",),
            ),
            models=("supertonic-3",),
        )
    return ProviderDescriptor(
        provider=provider,
        family="tts",
        adapter="openai_compatible",
        capabilities=ProviderCapabilities(
            modalities=frozenset({"tts"}),
            required_credentials=("api_key",),
            latency_profile="interactive",
            interruption_support=True,
            output_audio_format="pcm_f32_8000",
            usage_metadata=("duration", "model", "voice"),
        ),
    )
