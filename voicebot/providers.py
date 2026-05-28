from __future__ import annotations

from dataclasses import dataclass, field
import os
from typing import Any, Literal


SUPPORTED_STT_PROVIDERS = {
    "assemblyai",
    "aws",
    "azure",
    "cartesia",
    "deepgram",
    "elevenlabs",
    "fal",
    "fal-wizper",
    "gladia",
    "google",
    "gradium",
    "groq",
    "mistral",
    "nvidia",
    "openai",
    "openai-compatible",
    "sarvam",
    "smallest",
    "soniox",
    "speechmatics",
    "whisper",
    "xai",
}

SUPPORTED_TTS_PROVIDERS = {
    "async",
    "asyncai",
    "aws",
    "azure",
    "camb",
    "cartesia",
    "deepgram",
    "elevenlabs",
    "fish",
    "google",
    "gradium",
    "groq",
    "hume",
    "inworld",
    "kokoro",
    "lmnt",
    "minimax",
    "mistral",
    "neuphonic",
    "nvidia",
    "openai",
    "openai-compatible",
    "piper",
    "resemble",
    "rime",
    "sarvam",
    "smallest",
    "soniox",
    "speechmatics",
    "supertonic",
    "xai",
    "xtts",
}

SUPPORTED_AGENT_PROVIDERS = {
    "anthropic",
    "aws",
    "bedrock",
    "azure",
    "cerebras",
    "deepseek",
    "fireworks",
    "gemini",
    "google",
    "google-vertex",
    "grok",
    "groq",
    "mistral",
    "nebius",
    "novita",
    "nvidia",
    "ollama",
    "openai",
    "openai-chat",
    "openai-chat-compatible",
    "openai-responses",
    "openrouter",
    "perplexity",
    "qwen",
    "sambanova",
    "sarvam",
    "together",
    "xai",
}

STT_OPENAI_COMPATIBLE_PROVIDERS = {
    "openai",
    "openai-compatible",
    "groq",
    "mistral",
    "nvidia",
    "xai",
}

TTS_OPENAI_COMPATIBLE_PROVIDERS = {
    "openai",
    "openai-compatible",
    "groq",
    "mistral",
    "nvidia",
    "xai",
}

AGENT_CHAT_COMPATIBLE_PROVIDERS = {
    "azure",
    "cerebras",
    "deepseek",
    "fireworks",
    "grok",
    "groq",
    "mistral",
    "nebius",
    "novita",
    "nvidia",
    "ollama",
    "openai",
    "openai-chat",
    "openai-chat-compatible",
    "openrouter",
    "perplexity",
    "qwen",
    "sambanova",
    "sarvam",
    "together",
    "xai",
}

PROVIDER_KEY_ENVS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "assemblyai": "ASSEMBLYAI_API_KEY",
    "aws": "AWS_ACCESS_KEY_ID",
    "azure": "AZURE_OPENAI_API_KEY",
    "cartesia": "CARTESIA_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
    "deepgram": "DEEPGRAM_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "elevenlabs": "ELEVENLABS_API_KEY",
    "fal": "FAL_KEY",
    "fal-wizper": "FAL_KEY",
    "fireworks": "FIREWORKS_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "gladia": "GLADIA_API_KEY",
    "google": "GOOGLE_API_KEY",
    "gradium": "GRADIUM_API_KEY",
    "grok": "XAI_API_KEY",
    "groq": "GROQ_API_KEY",
    "hume": "HUME_API_KEY",
    "lmnt": "LMNT_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "nebius": "NEBIUS_API_KEY",
    "neuphonic": "NEUPHONIC_API_KEY",
    "novita": "NOVITA_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openai-chat": "OPENAI_API_KEY",
    "openai-responses": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "perplexity": "PERPLEXITY_API_KEY",
    "qwen": "DASHSCOPE_API_KEY",
    "resemble": "RESEMBLE_API_KEY",
    "rime": "RIME_API_KEY",
    "sambanova": "SAMBANOVA_API_KEY",
    "sarvam": "SARVAM_API_KEY",
    "smallest": "SMALLEST_API_KEY",
    "soniox": "SONIOX_API_KEY",
    "speechmatics": "SPEECHMATICS_API_KEY",
    "together": "TOGETHER_API_KEY",
    "xai": "XAI_API_KEY",
}

OPENAI_COMPATIBLE_BASE_URLS = {
    "cerebras": "https://api.cerebras.ai/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "fireworks": "https://api.fireworks.ai/inference/v1",
    "grok": "https://api.x.ai/v1",
    "groq": "https://api.groq.com/openai/v1",
    "mistral": "https://api.mistral.ai/v1",
    "nebius": "https://api.studio.nebius.com/v1",
    "novita": "https://api.novita.ai/v3/openai",
    "nvidia": "https://integrate.api.nvidia.com/v1",
    "ollama": "http://host.docker.internal:11434/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "perplexity": "https://api.perplexity.ai",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "sambanova": "https://api.sambanova.ai/v1",
    "together": "https://api.together.xyz/v1",
    "xai": "https://api.x.ai/v1",
}


@dataclass(frozen=True)
class ProviderCredentials:
    api_key: str
    base_url: str


ProviderModality = Literal[
    "stt",
    "streaming_stt",
    "tts",
    "streaming_tts",
    "agent",
    "speech_to_speech",
    "embeddings",
    "image_input",
    "video_input",
    "file_input",
    "chat",
    "visual_output",
    "avatar_video_output",
]

LatencyProfile = Literal["realtime", "interactive", "batch", "unknown"]


@dataclass(frozen=True)
class ProviderCapabilities:
    modalities: frozenset[ProviderModality]
    streaming: bool = False
    languages: tuple[str, ...] = ()
    required_credentials: tuple[str, ...] = ()
    latency_profile: LatencyProfile = "unknown"
    interruption_support: bool = False
    output_audio_format: str | None = None
    usage_metadata: tuple[str, ...] = ()
    native_tools: bool = False

    def supports(self, modality: ProviderModality) -> bool:
        return modality in self.modalities

    def to_dict(self) -> dict[str, Any]:
        return {
            "modalities": sorted(self.modalities),
            "streaming": self.streaming,
            "languages": list(self.languages),
            "required_credentials": list(self.required_credentials),
            "latency_profile": self.latency_profile,
            "interruption_support": self.interruption_support,
            "output_audio_format": self.output_audio_format,
            "usage_metadata": list(self.usage_metadata),
            "native_tools": self.native_tools,
        }


@dataclass(frozen=True)
class ProviderDescriptor:
    provider: str
    family: Literal["stt", "tts", "agent", "speech_to_speech", "embeddings"]
    adapter: Literal["native", "openai_compatible", "chat_compatible", "declared_only"]
    capabilities: ProviderCapabilities
    models: tuple[str, ...] = ()
    config: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "family": self.family,
            "adapter": self.adapter,
            "capabilities": self.capabilities.to_dict(),
            "models": list(self.models),
            "config": self.config,
        }


def normalize_provider(value: str) -> str:
    return value.strip().lower().replace("_", "-")


def provider_api_key(provider: str, explicit: str = "", fallback: str = "") -> str:
    if explicit:
        return explicit
    env_name = PROVIDER_KEY_ENVS.get(normalize_provider(provider))
    if env_name:
        value = os.getenv(env_name, "")
        if value:
            return value
    return fallback or os.getenv("OPENAI_API_KEY", "")


def provider_base_url(provider: str, explicit: str = "", fallback: str = "") -> str:
    if explicit:
        return explicit
    provider = normalize_provider(provider)
    return fallback or OPENAI_COMPATIBLE_BASE_URLS.get(provider, "")


def unsupported_provider_message(kind: str, provider: str, supported: set[str], adapter_hint: str) -> str:
    provider_list = ", ".join(sorted(supported))
    return (
        f"Unsupported {kind} provider adapter for '{provider}'. "
        f"Known {kind} provider names are: {provider_list}. {adapter_hint}"
    )
