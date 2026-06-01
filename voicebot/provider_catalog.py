from __future__ import annotations

from .providers import (
    AGENT_CHAT_COMPATIBLE_PROVIDERS,
    STT_HTTP_BATCH_PROVIDERS,
    STT_OPENAI_COMPATIBLE_PROVIDERS,
    TTS_HTTP_PROVIDERS,
    SUPPORTED_AGENT_PROVIDERS,
    SUPPORTED_STT_PROVIDERS,
    SUPPORTED_TTS_PROVIDERS,
    TTS_OPENAI_COMPATIBLE_PROVIDERS,
    ProviderCapabilities,
    ProviderDescriptor,
)


def provider_catalog() -> dict[str, dict[str, list[str]]]:
    stt_capabilities = _stt_capabilities()
    tts_capabilities = _tts_capabilities()
    agent_capabilities = _agent_capabilities()
    return {
        "stt": {
            "supported": sorted(SUPPORTED_STT_PROVIDERS),
            "native": sorted({"whisper", *STT_HTTP_BATCH_PROVIDERS}),
            "openai_compatible": sorted(STT_OPENAI_COMPATIBLE_PROVIDERS),
            "capabilities": {provider: descriptor.to_dict() for provider, descriptor in sorted(stt_capabilities.items())},
        },
        "tts": {
            "supported": sorted(SUPPORTED_TTS_PROVIDERS),
            "native": sorted({"supertonic", *TTS_HTTP_PROVIDERS}),
            "openai_compatible": sorted(TTS_OPENAI_COMPATIBLE_PROVIDERS),
            "capabilities": {provider: descriptor.to_dict() for provider, descriptor in sorted(tts_capabilities.items())},
        },
        "agent": {
            "supported": sorted(SUPPORTED_AGENT_PROVIDERS),
            "native": ["openai-responses"],
            "chat_compatible": sorted(AGENT_CHAT_COMPATIBLE_PROVIDERS),
            "capabilities": {
                provider: descriptor.to_dict() for provider, descriptor in sorted(agent_capabilities.items())
            },
        },
    }


def _stt_capabilities() -> dict[str, ProviderDescriptor]:
    descriptors: dict[str, ProviderDescriptor] = {
        "whisper": ProviderDescriptor(
            provider="whisper",
            family="stt",
            adapter="native",
            capabilities=ProviderCapabilities(
                modalities=frozenset({"stt"}),
                latency_profile="batch",
                interruption_support=True,
                usage_metadata=("duration", "language", "segments", "confidence"),
            ),
            models=("tiny", "base", "small", "medium", "large", "turbo"),
        )
    }
    for provider in STT_HTTP_BATCH_PROVIDERS:
        descriptors[provider] = ProviderDescriptor(
            provider=provider,
            family="stt",
            adapter="native",
            capabilities=ProviderCapabilities(
                modalities=frozenset({"stt"}),
                required_credentials=("api_key",),
                latency_profile="interactive",
                interruption_support=True,
                usage_metadata=("duration", "language", "request_id"),
            ),
            models=("nova-3", "nova-2", "base") if provider == "deepgram" else ("universal", "nano"),
            config={
                "default_base_url": "https://api.deepgram.com"
                if provider == "deepgram"
                else "https://api.assemblyai.com",
                "api_key_env": "DEEPGRAM_API_KEY" if provider == "deepgram" else "ASSEMBLYAI_API_KEY",
            },
        )
    for provider in STT_OPENAI_COMPATIBLE_PROVIDERS:
        descriptors[provider] = ProviderDescriptor(
            provider=provider,
            family="stt",
            adapter="openai_compatible",
            capabilities=ProviderCapabilities(
                modalities=frozenset({"stt"}),
                required_credentials=("api_key",),
                latency_profile="interactive",
                interruption_support=True,
                usage_metadata=("duration", "language", "segments"),
            ),
        )
    return descriptors


def _tts_capabilities() -> dict[str, ProviderDescriptor]:
    descriptors: dict[str, ProviderDescriptor] = {
        "supertonic": ProviderDescriptor(
            provider="supertonic",
            family="tts",
            adapter="native",
            capabilities=ProviderCapabilities(
                modalities=frozenset({"tts"}),
                latency_profile="batch",
                interruption_support=True,
                output_audio_format="pcm_f32_8000",
                usage_metadata=("duration",),
            ),
            models=("supertonic-3",),
        )
    }
    for provider in TTS_HTTP_PROVIDERS:
        descriptors[provider] = ProviderDescriptor(
            provider=provider,
            family="tts",
            adapter="native",
            capabilities=ProviderCapabilities(
                modalities=frozenset({"tts"}),
                required_credentials=("api_key",),
                latency_profile="interactive",
                interruption_support=True,
                output_audio_format="pcm_f32_8000",
                usage_metadata=("duration", "model", "voice"),
            ),
            models=(
                ("aura-2-thalia-en", "aura-2-asteria-en", "aura-2-luna-en")
                if provider == "deepgram"
                else ("eleven_flash_v2_5", "eleven_turbo_v2_5", "eleven_multilingual_v2")
            ),
            config={
                "default_base_url": "https://api.deepgram.com"
                if provider == "deepgram"
                else "https://api.elevenlabs.io",
                "api_key_env": "DEEPGRAM_API_KEY" if provider == "deepgram" else "ELEVENLABS_API_KEY",
            },
        )
    for provider in TTS_OPENAI_COMPATIBLE_PROVIDERS:
        descriptors[provider] = ProviderDescriptor(
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
    return descriptors


def _agent_capabilities() -> dict[str, ProviderDescriptor]:
    descriptors = {
        "openai-responses": ProviderDescriptor(
            provider="openai-responses",
            family="agent",
            adapter="native",
            capabilities=ProviderCapabilities(
                modalities=frozenset({"agent"}),
                required_credentials=("api_key",),
                latency_profile="interactive",
                usage_metadata=("input_tokens", "output_tokens", "tool_calls"),
                native_tools=True,
            ),
        ),
        "anthropic": ProviderDescriptor(
            provider="anthropic",
            family="agent",
            adapter="native",
            capabilities=ProviderCapabilities(
                modalities=frozenset({"agent"}),
                required_credentials=("api_key",),
                latency_profile="interactive",
                usage_metadata=("input_tokens", "output_tokens", "tool_calls"),
                native_tools=True,
            ),
        ),
    }
    for provider in AGENT_CHAT_COMPATIBLE_PROVIDERS:
        descriptors[provider] = ProviderDescriptor(
            provider=provider,
            family="agent",
            adapter="chat_compatible",
            capabilities=ProviderCapabilities(
                modalities=frozenset({"agent"}),
                required_credentials=("api_key",),
                latency_profile="interactive",
                usage_metadata=("input_tokens", "output_tokens"),
                native_tools=False,
            ),
        )
    return descriptors
