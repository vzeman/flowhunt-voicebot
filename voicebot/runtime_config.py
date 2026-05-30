from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from .provider_config import VoicebotProviderConfig, provider_config_to_dict, provider_selection_plan, selection_plan_to_dict


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class VoicebotPromptConfig:
    greeting: str = "The call has connected. Greet the caller and ask how you can help."
    system_prompt: str = ""
    stt_prompt: str = ""
    language: str = "en"

    def __post_init__(self) -> None:
        if not self.greeting.strip():
            raise ValueError("greeting prompt is required")
        if not self.language.strip():
            raise ValueError("language is required")

    def as_dict(self) -> dict[str, Any]:
        return {
            "greeting": self.greeting,
            "system_prompt": self.system_prompt,
            "stt_prompt": self.stt_prompt,
            "language": self.language,
        }


@dataclass(frozen=True)
class VoicebotRealtimeConfig:
    silence_ms: int = 450
    vad_start_ms: int = 60
    min_seconds: float = 0.35
    max_seconds: float = 20.0
    start_threshold: float = 0.020
    stop_threshold: float = 0.010
    barge_in_threshold: float = 0.08
    echo_tail_ms: int = 300
    max_reply_chars: int = 240
    tts_chunk_chars: int = 90

    def __post_init__(self) -> None:
        if self.silence_ms < 100:
            raise ValueError("silence_ms must be at least 100")
        if self.vad_start_ms < 0:
            raise ValueError("vad_start_ms must not be negative")
        if self.min_seconds <= 0 or self.max_seconds <= 0 or self.min_seconds > self.max_seconds:
            raise ValueError("min_seconds and max_seconds must define a valid range")
        for name in ("start_threshold", "stop_threshold", "barge_in_threshold"):
            value = float(getattr(self, name))
            if value < 0:
                raise ValueError(f"{name} must not be negative")
        if self.echo_tail_ms < 0:
            raise ValueError("echo_tail_ms must not be negative")
        if self.max_reply_chars < 1:
            raise ValueError("max_reply_chars must be positive")
        if self.tts_chunk_chars < 1:
            raise ValueError("tts_chunk_chars must be positive")

    def as_dict(self) -> dict[str, Any]:
        return {
            "silence_ms": self.silence_ms,
            "vad_start_ms": self.vad_start_ms,
            "min_seconds": self.min_seconds,
            "max_seconds": self.max_seconds,
            "start_threshold": self.start_threshold,
            "stop_threshold": self.stop_threshold,
            "barge_in_threshold": self.barge_in_threshold,
            "echo_tail_ms": self.echo_tail_ms,
            "max_reply_chars": self.max_reply_chars,
            "tts_chunk_chars": self.tts_chunk_chars,
        }


@dataclass(frozen=True)
class VoicebotQuotaConfig:
    max_concurrent_sessions: int = 1
    max_provider_inflight: int = 10
    enabled_actions: tuple[str, ...] = (
        "say",
        "hangup_call",
        "transfer_call",
        "send_dtmf",
        "delegate_to_subagent",
        "invoke_flowhunt_flow",
    )

    def __post_init__(self) -> None:
        if self.max_concurrent_sessions < 1:
            raise ValueError("max_concurrent_sessions must be positive")
        if self.max_provider_inflight < 1:
            raise ValueError("max_provider_inflight must be positive")
        if any(not action.strip() for action in self.enabled_actions):
            raise ValueError("enabled_actions must not contain blank values")

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_concurrent_sessions": self.max_concurrent_sessions,
            "max_provider_inflight": self.max_provider_inflight,
            "enabled_actions": list(self.enabled_actions),
        }


@dataclass(frozen=True)
class VoicebotSubagentConfig:
    flowhunt_workspace_id: str = ""
    flowhunt_flow_id: str = ""
    flowhunt_project_id: str = ""
    complex_backend: str = "flow"

    def __post_init__(self) -> None:
        if self.complex_backend not in {"flow", "project", "disabled"}:
            raise ValueError("complex_backend must be flow, project, or disabled")

    def as_dict(self) -> dict[str, Any]:
        return {
            "flowhunt_workspace_id": self.flowhunt_workspace_id,
            "flowhunt_flow_id": self.flowhunt_flow_id,
            "flowhunt_project_id": self.flowhunt_project_id,
            "complex_backend": self.complex_backend,
        }


@dataclass(frozen=True)
class VoicebotRuntimeConfig:
    workspace_id: str
    voicebot_id: str
    config_version: int
    providers: VoicebotProviderConfig
    prompts: VoicebotPromptConfig = field(default_factory=VoicebotPromptConfig)
    realtime: VoicebotRealtimeConfig = field(default_factory=VoicebotRealtimeConfig)
    quotas: VoicebotQuotaConfig = field(default_factory=VoicebotQuotaConfig)
    subagents: VoicebotSubagentConfig = field(default_factory=VoicebotSubagentConfig)
    enabled: bool = True
    created_at: str = field(default_factory=utc_now_iso)
    activated_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        if not self.workspace_id.strip():
            raise ValueError("workspace_id is required")
        if not self.voicebot_id.strip():
            raise ValueError("voicebot_id is required")
        if self.config_version < 1:
            raise ValueError("config_version must be positive")
        if self.providers.workspace_id != self.workspace_id or self.providers.voicebot_id != self.voicebot_id:
            raise ValueError("provider config scope must match runtime config scope")
        _parse_timestamp(self.created_at)
        _parse_timestamp(self.activated_at)

    def next_version(self) -> VoicebotRuntimeConfig:
        return replace(self, config_version=self.config_version + 1, activated_at=utc_now_iso())

    def as_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "voicebot_id": self.voicebot_id,
            "config_version": self.config_version,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "activated_at": self.activated_at,
            "providers": provider_config_to_dict(self.providers),
            "selection_plan": selection_plan_to_dict(provider_selection_plan(self.providers)),
            "prompts": self.prompts.as_dict(),
            "realtime": self.realtime.as_dict(),
            "quotas": self.quotas.as_dict(),
            "subagents": self.subagents.as_dict(),
        }


class VoicebotRuntimeConfigStore:
    def __init__(self) -> None:
        self._configs: dict[tuple[str, str], VoicebotRuntimeConfig] = {}

    def save(self, config: VoicebotRuntimeConfig) -> VoicebotRuntimeConfig:
        existing = self.get(config.workspace_id, config.voicebot_id)
        if existing is not None and config.config_version <= existing.config_version:
            config = replace(config, config_version=existing.config_version + 1, activated_at=utc_now_iso())
        self._configs[(config.workspace_id, config.voicebot_id)] = config
        return config

    def get(self, workspace_id: str, voicebot_id: str) -> VoicebotRuntimeConfig | None:
        return self._configs.get((workspace_id, voicebot_id))

    def list(self, workspace_id: str | None = None) -> list[VoicebotRuntimeConfig]:
        configs = self._configs.values()
        if workspace_id is not None:
            configs = [config for config in configs if config.workspace_id == workspace_id]
        return sorted(configs, key=lambda config: (config.workspace_id, config.voicebot_id, config.config_version))


def runtime_config_to_dict(config: VoicebotRuntimeConfig) -> dict[str, Any]:
    return config.as_dict()


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamps must include timezone")
    return parsed.astimezone(UTC)
