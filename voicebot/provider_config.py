from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import threading
from typing import Literal, get_args

from .providers import ProviderDescriptor, normalize_provider


ProviderFamily = Literal["stt", "tts", "agent"]


@dataclass(frozen=True)
class SecretReference:
    name: str
    workspace_id: str

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("secret reference name is required")
        if not self.workspace_id.strip():
            raise ValueError("secret reference workspace_id is required")


@dataclass(frozen=True)
class ProviderChoice:
    family: ProviderFamily
    provider: str
    model: str | None = None
    secret_ref: SecretReference | None = None
    fallback_provider: str | None = None
    config: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.family not in get_args(ProviderFamily):
            raise ValueError(f"unsupported provider family: {self.family}")
        if not self.provider.strip():
            raise ValueError("provider is required")
        if self.model is not None and not self.model.strip():
            raise ValueError("model must not be blank")
        if self.fallback_provider is not None and not self.fallback_provider.strip():
            raise ValueError("fallback_provider must not be blank")

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
        if not self.workspace_id.strip():
            raise ValueError("workspace_id is required")
        if not self.voicebot_id.strip():
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
        self._lock = threading.RLock()
        self._configs: dict[tuple[str, str], VoicebotProviderConfig] = {}

    def save(self, config: VoicebotProviderConfig) -> VoicebotProviderConfig:
        with self._lock:
            self._configs[(config.workspace_id, config.voicebot_id)] = config
        return config

    def get(self, workspace_id: str, voicebot_id: str) -> VoicebotProviderConfig | None:
        with self._lock:
            return self._configs.get((workspace_id, voicebot_id))

    def list(self, workspace_id: str | None = None) -> list[VoicebotProviderConfig]:
        with self._lock:
            configs = list(self._configs.values())
        if workspace_id is not None:
            configs = [config for config in configs if config.workspace_id == workspace_id]
        return sorted(configs, key=lambda config: (config.workspace_id, config.voicebot_id))


class JsonProviderConfigStore(ProviderConfigStore):
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.load_diagnostics: dict[str, int] = {
            "loaded_configs": 0,
            "skipped_malformed_json": 0,
            "skipped_invalid_configs": 0,
            "skipped_duplicate_configs": 0,
        }
        super().__init__()
        self._load()

    def save(self, config: VoicebotProviderConfig) -> VoicebotProviderConfig:
        saved = super().save(config)
        self._save()
        return saved

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.load_diagnostics["skipped_malformed_json"] += 1
            return
        seen: set[tuple[str, str]] = set()
        configs = payload.get("configs", [])
        if not isinstance(configs, list):
            self.load_diagnostics["skipped_invalid_configs"] += 1
            return
        for item in configs:
            try:
                config = provider_config_from_dict(item)
            except (TypeError, ValueError, KeyError):
                self.load_diagnostics["skipped_invalid_configs"] += 1
                continue
            key = (config.workspace_id, config.voicebot_id)
            if key in seen:
                self.load_diagnostics["skipped_duplicate_configs"] += 1
                continue
            seen.add(key)
            self._configs[key] = config
            self.load_diagnostics["loaded_configs"] += 1

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "configs": [provider_config_to_dict(config) for config in self.list()]}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
        tmp.replace(self.path)


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


def provider_config_from_dict(data: dict) -> VoicebotProviderConfig:
    return VoicebotProviderConfig(
        workspace_id=str(data["workspace_id"]),
        voicebot_id=str(data["voicebot_id"]),
        stt=provider_choice_from_dict(data["stt"], "stt"),
        tts=provider_choice_from_dict(data["tts"], "tts"),
        agent=provider_choice_from_dict(data["agent"], "agent"),
    )


def provider_choice_from_dict(data: dict, expected_family: ProviderFamily) -> ProviderChoice:
    if not isinstance(data, dict):
        raise ValueError("provider choice must be an object")
    family = str(data.get("family") or expected_family)
    return ProviderChoice(
        family=family,  # type: ignore[arg-type]
        provider=str(data["provider"]),
        model=str(data["model"]) if data.get("model") is not None else None,
        secret_ref=secret_reference_from_dict(data["secret_ref"]) if data.get("secret_ref") else None,
        fallback_provider=str(data["fallback_provider"]) if data.get("fallback_provider") is not None else None,
        config=dict(data.get("config") or {}),
    )


def secret_reference_from_dict(data: dict) -> SecretReference:
    if not isinstance(data, dict):
        raise ValueError("secret reference must be an object")
    return SecretReference(name=str(data["name"]), workspace_id=str(data["workspace_id"]))


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
        if choice.model and descriptor.models and choice.model not in descriptor.models:
            issues.append(ProviderValidationIssue(family, provider, f"model is not supported by provider: {choice.model}"))
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
