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


class ProviderConfigStore:
    def __init__(self) -> None:
        self._configs: dict[tuple[str, str], VoicebotProviderConfig] = {}

    def save(self, config: VoicebotProviderConfig) -> VoicebotProviderConfig:
        self._configs[(config.workspace_id, config.voicebot_id)] = config
        return config

    def get(self, workspace_id: str, voicebot_id: str) -> VoicebotProviderConfig | None:
        return self._configs.get((workspace_id, voicebot_id))

    def list(self, workspace_id: str | None = None) -> list[VoicebotProviderConfig]:
        configs = self._configs.values()
        if workspace_id is not None:
            configs = [config for config in configs if config.workspace_id == workspace_id]
        return sorted(configs, key=lambda config: (config.workspace_id, config.voicebot_id))


def provider_config_to_dict(config: VoicebotProviderConfig) -> dict:
    return {
        "workspace_id": config.workspace_id,
        "voicebot_id": config.voicebot_id,
        "stt": provider_choice_to_dict(config.stt),
        "tts": provider_choice_to_dict(config.tts),
        "agent": provider_choice_to_dict(config.agent),
    }


def provider_choice_to_dict(choice: ProviderChoice) -> dict:
    return {
        "family": choice.family,
        "provider": choice.provider,
        "model": choice.model,
        "secret_ref": secret_reference_to_dict(choice.secret_ref) if choice.secret_ref else None,
        "fallback_provider": choice.fallback_provider,
        "config": choice.config,
    }


def secret_reference_to_dict(secret: SecretReference) -> dict:
    return {"name": secret.name, "workspace_id": secret.workspace_id}


def validation_issue_to_dict(issue: ProviderValidationIssue) -> dict:
    return {"family": issue.family, "provider": issue.provider, "message": issue.message}


def selection_plan_to_dict(plan: ProviderSelectionPlan) -> dict:
    return {
        "workspace_id": plan.workspace_id,
        "voicebot_id": plan.voicebot_id,
        "providers": plan.providers,
        "fallbacks": plan.fallbacks,
        "models": plan.models,
    }


def validate_provider_config(
    config: VoicebotProviderConfig,
    descriptors: dict[ProviderFamily, dict[str, ProviderDescriptor]],
) -> list[ProviderValidationIssue]:
    issues: list[ProviderValidationIssue] = []
    for family in ("stt", "tts", "agent"):
        choice = config.choice(family)
        provider = choice.normalized_provider()
        if choice.family != family:
            issues.append(ProviderValidationIssue(family, provider, f"choice family must be {family}"))
        descriptor = descriptors.get(family, {}).get(provider)
        if descriptor is None:
            issues.append(ProviderValidationIssue(family, provider, "provider is not registered"))
            continue
        required = descriptor.capabilities.required_credentials
        if required and choice.secret_ref is None:
            issues.append(ProviderValidationIssue(family, provider, "provider requires a secret reference"))
        if choice.secret_ref is not None and choice.secret_ref.workspace_id != config.workspace_id:
            issues.append(ProviderValidationIssue(family, provider, "secret reference belongs to a different workspace"))
        fallback = choice.normalized_fallback()
        if fallback:
            if fallback == provider:
                issues.append(ProviderValidationIssue(family, fallback, "fallback provider must be different from provider"))
                continue
            fallback_descriptor = descriptors.get(family, {}).get(fallback)
            if fallback_descriptor is None:
                issues.append(ProviderValidationIssue(family, fallback, "fallback provider is not registered"))
            elif fallback_descriptor.capabilities.required_credentials and choice.secret_ref is None:
                issues.append(ProviderValidationIssue(family, fallback, "fallback provider requires a secret reference"))
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
