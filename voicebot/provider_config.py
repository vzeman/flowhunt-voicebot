from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .providers import ProviderDescriptor, normalize_provider


ProviderFamily = Literal["stt", "tts", "agent"]


@dataclass(frozen=True)
class SecretReference:
    name: str
    workspace_id: str

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("secret reference name is required")
        if not self.workspace_id:
            raise ValueError("secret reference workspace_id is required")


@dataclass(frozen=True)
class ProviderChoice:
    family: ProviderFamily
    provider: str
    model: str | None = None
    secret_ref: SecretReference | None = None
    fallback_provider: str | None = None
    config: dict = field(default_factory=dict)

    def normalized_provider(self) -> str:
        return normalize_provider(self.provider)

    def normalized_fallback(self) -> str | None:
        return normalize_provider(self.fallback_provider) if self.fallback_provider else None


@dataclass(frozen=True)
class VoicebotProviderConfig:
    workspace_id: str
    voicebot_id: str
    stt: ProviderChoice
    tts: ProviderChoice
    agent: ProviderChoice

    def __post_init__(self) -> None:
        if not self.workspace_id:
            raise ValueError("workspace_id is required")
        if not self.voicebot_id:
            raise ValueError("voicebot_id is required")

    def choice(self, family: ProviderFamily) -> ProviderChoice:
        return {"stt": self.stt, "tts": self.tts, "agent": self.agent}[family]


@dataclass(frozen=True)
class ProviderValidationIssue:
    family: ProviderFamily
    provider: str
    message: str


@dataclass(frozen=True)
class ProviderSelectionPlan:
    workspace_id: str
    voicebot_id: str
    providers: dict[ProviderFamily, str]
    fallbacks: dict[ProviderFamily, str]
    models: dict[ProviderFamily, str]


def validate_provider_config(
    config: VoicebotProviderConfig,
    descriptors: dict[ProviderFamily, dict[str, ProviderDescriptor]],
) -> list[ProviderValidationIssue]:
    issues: list[ProviderValidationIssue] = []
    for family in ("stt", "tts", "agent"):
        choice = config.choice(family)
        provider = choice.normalized_provider()
        descriptor = descriptors.get(family, {}).get(provider)
        if descriptor is None:
            issues.append(ProviderValidationIssue(family, provider, "provider is not registered"))
            continue
        required = descriptor.capabilities.required_credentials
        if required and choice.secret_ref is None:
            issues.append(ProviderValidationIssue(family, provider, "provider requires a secret reference"))
        fallback = choice.normalized_fallback()
        if fallback and fallback not in descriptors.get(family, {}):
            issues.append(ProviderValidationIssue(family, fallback, "fallback provider is not registered"))
    return issues


def provider_selection_plan(config: VoicebotProviderConfig) -> ProviderSelectionPlan:
    providers: dict[ProviderFamily, str] = {}
    fallbacks: dict[ProviderFamily, str] = {}
    models: dict[ProviderFamily, str] = {}
    for family in ("stt", "tts", "agent"):
        choice = config.choice(family)
        providers[family] = choice.normalized_provider()
        fallback = choice.normalized_fallback()
        if fallback:
            fallbacks[family] = fallback
        if choice.model:
            models[family] = choice.model
    return ProviderSelectionPlan(
        workspace_id=config.workspace_id,
        voicebot_id=config.voicebot_id,
        providers=providers,
        fallbacks=fallbacks,
        models=models,
    )
