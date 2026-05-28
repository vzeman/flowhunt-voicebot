from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, get_args

from .events import EventStore, VoicebotEvent
from .execution_model import ExecutionIds, ExecutionScope


ProviderCallKind = Literal["stt", "tts", "agent", "subagent", "speech_to_speech", "embedding"]


@dataclass(frozen=True)
class ProviderCallContext:
    provider: str
    kind: ProviderCallKind
    scope: ExecutionScope
    ids: ExecutionIds = field(default_factory=ExecutionIds)
    model: str | None = None

    def __post_init__(self) -> None:
        if not self.provider.strip():
            raise ValueError("provider is required")
        if self.kind not in get_args(ProviderCallKind):
            raise ValueError(f"unsupported provider call kind: {self.kind}")
        if self.model is not None and not self.model.strip():
            raise ValueError("model must not be blank")

    def event_data(self) -> dict[str, Any]:
        data = {
            "provider": self.provider,
            "provider_kind": self.kind,
            **self.scope.to_data(),
            **self.ids.to_data(),
        }
        if self.model:
            data["model"] = self.model
        return data


@dataclass(frozen=True)
class ProviderFailure:
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.code.strip():
            raise ValueError("provider failure code is required")
        if not self.message.strip():
            raise ValueError("provider failure message is required")

    def event_data(self) -> dict[str, Any]:
        return {
            "error_code": self.code,
            "error": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }


def record_provider_latency(
    events: EventStore,
    context: ProviderCallContext,
    latency_seconds: float,
) -> VoicebotEvent:
    if latency_seconds < 0:
        raise ValueError("latency_seconds must be greater than or equal to 0")
    return events.append_scoped(
        context.scope,
        "metrics",
        {
            "name": f"{context.kind}_provider_latency_seconds",
            "value": latency_seconds,
            "provider": context.provider,
            "provider_kind": context.kind,
            **({"model": context.model} if context.model else {}),
        },
        context.ids,
    )


def record_provider_failure(
    events: EventStore,
    context: ProviderCallContext,
    failure: ProviderFailure,
) -> VoicebotEvent:
    return events.append_scoped(
        context.scope,
        "provider_call_failed",
        {
            **context.event_data(),
            **failure.event_data(),
        },
        context.ids,
    )
