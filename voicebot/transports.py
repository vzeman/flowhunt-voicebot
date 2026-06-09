from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, get_args, runtime_checkable

from .multimodal import ModalityCapabilities
from .workspace_model import WorkspaceScope


TransportKind = Literal[
    "asterisk_audiosocket",
    "webrtc",
    "local",
    "twilio",
    "telnyx",
    "vonage",
    "livekit",
    "daily",
]

TransportHealthStatus = Literal["ready", "disabled", "stopped", "degraded"]

CallControlAction = Literal[
    "hangup",
    "transfer",
    "send_dtmf",
    "stop_playback",
    "read_transcript",
]


@dataclass(frozen=True)
class CallRoute:
    workspace_id: str | None = None
    voicebot_id: str | None = None
    trunk_id: str | None = None
    external_call_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_metadata(cls, metadata: dict[str, Any] | None) -> "CallRoute":
        payload = dict(metadata or {})
        return cls(
            workspace_id=_optional_str(payload.pop("workspace_id", None)),
            voicebot_id=_optional_str(payload.pop("voicebot_id", None)),
            trunk_id=_optional_str(payload.pop("trunk_id", None)),
            external_call_id=_optional_str(payload.pop("external_call_id", None)),
            metadata=payload,
        )

    def as_event_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        for key in ("workspace_id", "voicebot_id", "trunk_id", "external_call_id"):
            value = getattr(self, key)
            if value:
                data[key] = value
        if self.metadata:
            data["metadata"] = self.metadata
        return data

    def require_workspace_scope(self, session_id: str) -> WorkspaceScope:
        if not self.workspace_id:
            raise ValueError("workspace_id is required for routed media session")
        if not self.voicebot_id:
            raise ValueError("voicebot_id is required for routed media session")
        if not session_id:
            raise ValueError("session_id is required for routed media session")
        return WorkspaceScope(self.workspace_id, self.voicebot_id, session_id)


@dataclass(frozen=True)
class TransportCapabilities:
    call_control: frozenset[CallControlAction] = frozenset()
    inbound_audio: bool = True
    outbound_audio: bool = True
    interruptible_playback: bool = True
    concurrent_sessions: bool = True
    modalities: ModalityCapabilities = field(default_factory=ModalityCapabilities)

    def __post_init__(self) -> None:
        supported_actions = set(get_args(CallControlAction))
        invalid_actions = sorted(action for action in self.call_control if action not in supported_actions)
        if invalid_actions:
            raise ValueError(f"unsupported call-control actions: {', '.join(invalid_actions)}")

    def supports(self, action: CallControlAction) -> bool:
        return action in self.call_control


ASTERISK_AUDIOSOCKET_CAPABILITIES = TransportCapabilities(
    call_control=frozenset({"hangup", "transfer", "send_dtmf", "stop_playback", "read_transcript"}),
)

WEBRTC_CAPABILITIES = TransportCapabilities(
    call_control=frozenset({"hangup", "stop_playback", "read_transcript"}),
)


TRANSPORT_CAPABILITIES: dict[TransportKind, TransportCapabilities] = {
    "asterisk_audiosocket": ASTERISK_AUDIOSOCKET_CAPABILITIES,
    "webrtc": WEBRTC_CAPABILITIES,
    "local": TransportCapabilities(),
    "twilio": TransportCapabilities(),
    "telnyx": TransportCapabilities(),
    "vonage": TransportCapabilities(),
    "livekit": WEBRTC_CAPABILITIES,
    "daily": WEBRTC_CAPABILITIES,
}

TRANSPORT_ADAPTER_CONTRACTS: dict[TransportKind, str] = {
    "twilio": "hosted_telephony_webhook",
    "telnyx": "hosted_telephony_webhook",
    "vonage": "hosted_telephony_webhook",
    "livekit": "hosted_realtime_media_session",
    "daily": "hosted_realtime_media_session",
}

TRANSPORT_UNAVAILABLE_REASONS: dict[TransportKind, str] = {
    "twilio": "Twilio webhook/media adapter is planned and not wired to runtime startup yet.",
    "telnyx": "Telnyx webhook/media adapter is planned and not wired to runtime startup yet.",
    "vonage": "Vonage webhook/media adapter is planned and not wired to runtime startup yet.",
    "livekit": "LiveKit media-session adapter is planned and not wired to runtime startup yet.",
    "daily": "Daily media-session adapter is planned and not wired to runtime startup yet.",
}

HOSTED_TELEPHONY_TRANSPORTS: frozenset[TransportKind] = frozenset({"twilio", "telnyx", "vonage"})


@dataclass(frozen=True)
class MediaSessionDescriptor:
    call_id: str
    transport: TransportKind
    route: CallRoute = field(default_factory=CallRoute)
    capabilities: TransportCapabilities = field(default_factory=TransportCapabilities)
    sample_rate: int = 8_000
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.call_id:
            raise ValueError("call_id is required for media session descriptor")
        if self.transport not in get_args(TransportKind):
            raise ValueError(f"unsupported transport kind: {self.transport}")
        if self.sample_rate <= 0:
            raise ValueError("sample_rate must be greater than 0")

    def lifecycle_event_data(self) -> dict[str, Any]:
        return {
            "transport": self.transport,
            "sample_rate": self.sample_rate,
            **self.route.as_event_data(),
            **self.metadata,
        }

    def require_workspace_scope(self) -> WorkspaceScope:
        return self.route.require_workspace_scope(self.call_id)


@dataclass(frozen=True)
class HostedTelephonyWebhookSessionRequest:
    transport: TransportKind
    provider_call_id: str
    workspace_id: str
    voicebot_id: str
    trunk_id: str | None = None
    media_stream_url: str | None = None
    control_callback_url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.transport not in HOSTED_TELEPHONY_TRANSPORTS:
            raise ValueError(f"hosted telephony transport is required: {self.transport}")
        for field_name in ("provider_call_id", "workspace_id", "voicebot_id"):
            if not str(getattr(self, field_name) or "").strip():
                raise ValueError(f"{field_name} is required")
        if self.media_stream_url is not None and not self.media_stream_url.strip():
            raise ValueError("media_stream_url must not be blank")
        if self.control_callback_url is not None and not self.control_callback_url.strip():
            raise ValueError("control_callback_url must not be blank")

    @property
    def call_id(self) -> str:
        return f"{self.transport}-{self.provider_call_id}"

    def route(self) -> CallRoute:
        return CallRoute(
            workspace_id=self.workspace_id,
            voicebot_id=self.voicebot_id,
            trunk_id=self.trunk_id,
            external_call_id=self.provider_call_id,
            metadata=dict(self.metadata),
        )

    def descriptor(self, capabilities: TransportCapabilities | None = None, *, sample_rate: int = 8_000) -> MediaSessionDescriptor:
        metadata = {
            "provider_call_id": self.provider_call_id,
            **({"media_stream_url": self.media_stream_url} if self.media_stream_url else {}),
            **({"control_callback_url": self.control_callback_url} if self.control_callback_url else {}),
        }
        return MediaSessionDescriptor(
            call_id=self.call_id,
            transport=self.transport,
            route=self.route(),
            capabilities=capabilities or TRANSPORT_CAPABILITIES[self.transport],
            sample_rate=sample_rate,
            metadata=metadata,
        )


@dataclass(frozen=True)
class CallControlRequest:
    call_id: str
    action: CallControlAction
    data: dict[str, Any] = field(default_factory=dict)

    def as_event_data(self) -> dict[str, Any]:
        return {"call_id": self.call_id, "action": self.action, **self.data}


@dataclass(frozen=True)
class CallControlResult:
    call_id: str
    action: CallControlAction
    ok: bool
    reason: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def as_event_data(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "call_id": self.call_id,
            "action": self.action,
            "ok": self.ok,
            **self.data,
        }
        if self.reason:
            payload["reason"] = self.reason
        return payload

    @classmethod
    def unsupported(cls, request: CallControlRequest, transport: TransportKind) -> "CallControlResult":
        return cls(
            call_id=request.call_id,
            action=request.action,
            ok=False,
            reason=f"{request.action} is not supported by {transport}",
            data={"transport": transport},
        )


@dataclass(frozen=True)
class TransportHealth:
    kind: TransportKind
    ok: bool
    status: TransportHealthStatus
    active_sessions: int = 0
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in get_args(TransportKind):
            raise ValueError(f"unsupported transport kind: {self.kind}")
        if self.status not in get_args(TransportHealthStatus):
            raise ValueError(f"unsupported transport health status: {self.status}")
        if self.active_sessions < 0:
            raise ValueError("active_sessions must not be negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ok": self.ok,
            "status": self.status,
            "active_sessions": self.active_sessions,
            "details": self.details,
        }


@runtime_checkable
class MediaTransport(Protocol):
    kind: TransportKind
    capabilities: TransportCapabilities

    def start(self) -> TransportHealth:
        ...

    def create_session(self, call_id: str, metadata: dict[str, Any] | None = None) -> MediaSessionDescriptor:
        ...

    def describe_session(self, call_id: str, metadata: dict[str, Any] | None = None) -> MediaSessionDescriptor:
        ...

    def receive_inbound_media(self, call_id: str, payload: bytes, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        ...

    def send_outbound_media(self, call_id: str, payload: bytes, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        ...

    def execute_call_control(self, request: CallControlRequest) -> CallControlResult:
        ...

    def health(self) -> TransportHealth:
        ...

    def shutdown(self) -> TransportHealth:
        ...


@dataclass(frozen=True)
class TransportDefinition:
    kind: TransportKind
    capabilities: TransportCapabilities
    implemented: bool
    enabled: bool = True
    transport: MediaTransport | None = None
    adapter_contract: str | None = None
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in get_args(TransportKind):
            raise ValueError(f"unsupported transport kind: {self.kind}")
        if self.implemented and self.transport is None:
            raise ValueError(f"implemented transport requires adapter: {self.kind}")
        if not self.implemented and self.transport is not None:
            raise ValueError(f"planned transport must not provide adapter: {self.kind}")

    def to_dict(self, *, include_health: bool = False) -> dict[str, Any]:
        payload = {
            "kind": self.kind,
            "capabilities": transport_capabilities_to_dict(self.capabilities),
            "implemented": self.implemented,
            "enabled": self.enabled,
            "status": "available" if self.implemented else "planned",
        }
        if self.adapter_contract:
            payload["adapter_contract"] = self.adapter_contract
        if self.unavailable_reason:
            payload["unavailable_reason"] = self.unavailable_reason
        if include_health and self.transport is not None:
            payload["health"] = self.transport.health().to_dict()
        return payload


class TransportRegistry:
    def __init__(self, definitions: list[TransportDefinition] | None = None) -> None:
        self._definitions: dict[TransportKind, TransportDefinition] = {}
        for definition in definitions or []:
            self.register_definition(definition)

    def register_definition(self, definition: TransportDefinition) -> None:
        if definition.kind in self._definitions:
            raise ValueError(f"transport is already registered: {definition.kind}")
        self._definitions[definition.kind] = definition

    def register(self, transport: MediaTransport, *, enabled: bool = True) -> None:
        self.register_definition(
            TransportDefinition(
                kind=transport.kind,
                capabilities=transport.capabilities,
                implemented=True,
                enabled=enabled,
                transport=transport,
            )
        )

    def register_planned(
        self,
        kind: TransportKind,
        capabilities: TransportCapabilities,
        *,
        enabled: bool = False,
        adapter_contract: str | None = None,
        unavailable_reason: str | None = None,
    ) -> None:
        self.register_definition(
            TransportDefinition(
                kind=kind,
                capabilities=capabilities,
                implemented=False,
                enabled=enabled,
                transport=None,
                adapter_contract=adapter_contract,
                unavailable_reason=unavailable_reason,
            )
        )

    def get(self, kind: TransportKind, *, require_enabled: bool = True) -> MediaTransport:
        definition = self._definitions.get(kind)
        if definition is None:
            raise KeyError(f"transport is not registered: {kind}")
        if not definition.implemented or definition.transport is None:
            raise ValueError(f"transport is not implemented: {kind}")
        if require_enabled and not definition.enabled:
            raise ValueError(f"transport is not enabled: {kind}")
        return definition.transport

    def implemented(self) -> tuple[TransportDefinition, ...]:
        return tuple(definition for definition in self.definitions() if definition.implemented)

    def enabled(self) -> tuple[MediaTransport, ...]:
        return tuple(definition.transport for definition in self.implemented() if definition.enabled and definition.transport is not None)

    def start_enabled(self) -> dict[TransportKind, dict[str, Any]]:
        return {transport.kind: transport.start().to_dict() for transport in self.enabled()}

    def shutdown_enabled(self) -> dict[TransportKind, dict[str, Any]]:
        return {transport.kind: transport.shutdown().to_dict() for transport in self.enabled()}

    def definitions(self) -> tuple[TransportDefinition, ...]:
        return tuple(self._definitions[kind] for kind in sorted(self._definitions))

    def to_dict(self, *, include_health: bool = False) -> dict[str, Any]:
        return {
            "transports": {
                definition.kind: definition.to_dict(include_health=include_health)
                for definition in self.definitions()
            }
        }


class StaticMediaTransport:
    def __init__(
        self,
        kind: TransportKind,
        capabilities: TransportCapabilities,
        *,
        sample_rate: int = 8_000,
    ) -> None:
        if kind not in get_args(TransportKind):
            raise ValueError(f"unsupported transport kind: {kind}")
        if sample_rate <= 0:
            raise ValueError("sample_rate must be greater than 0")
        self.kind = kind
        self.capabilities = capabilities
        self.sample_rate = sample_rate
        self._shutdown = False
        self._started = False
        self._sessions: dict[str, MediaSessionDescriptor] = {}

    def start(self) -> TransportHealth:
        self._started = True
        self._shutdown = False
        return self.health()

    def create_session(self, call_id: str, metadata: dict[str, Any] | None = None) -> MediaSessionDescriptor:
        descriptor = self.describe_session(call_id, metadata)
        self._sessions[call_id] = descriptor
        return descriptor

    def describe_session(self, call_id: str, metadata: dict[str, Any] | None = None) -> MediaSessionDescriptor:
        route = CallRoute.from_metadata(metadata)
        return MediaSessionDescriptor(
            call_id=call_id,
            transport=self.kind,
            route=route,
            capabilities=self.capabilities,
            sample_rate=self.sample_rate,
        )

    def receive_inbound_media(self, call_id: str, payload: bytes, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._media_event("inbound", call_id, payload, metadata)

    def send_outbound_media(self, call_id: str, payload: bytes, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._media_event("outbound", call_id, payload, metadata)

    def execute_call_control(self, request: CallControlRequest) -> CallControlResult:
        if self._shutdown:
            return CallControlResult(
                call_id=request.call_id,
                action=request.action,
                ok=False,
                reason=f"{self.kind} transport is stopped",
                data={"transport": self.kind},
            )
        if not self.capabilities.supports(request.action):
            return CallControlResult.unsupported(request, self.kind)
        return CallControlResult(
            call_id=request.call_id,
            action=request.action,
            ok=True,
            data={"transport": self.kind, **request.data},
        )

    def health(self) -> TransportHealth:
        return TransportHealth(
            kind=self.kind,
            ok=not self._shutdown,
            status="stopped" if self._shutdown else "ready",
            active_sessions=len(self._sessions),
            details={"sample_rate": self.sample_rate, "started": self._started},
        )

    def shutdown(self) -> TransportHealth:
        self._shutdown = True
        self._sessions.clear()
        return self.health()

    def _media_event(
        self,
        direction: Literal["inbound", "outbound"],
        call_id: str,
        payload: bytes,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._shutdown:
            raise RuntimeError(f"{self.kind} transport is stopped")
        if not call_id:
            raise ValueError("call_id is required for media")
        return {
            "transport": self.kind,
            "call_id": call_id,
            "direction": direction,
            "byte_count": len(payload),
            "metadata": dict(metadata or {}),
        }


def transport_capabilities_to_dict(capabilities: TransportCapabilities) -> dict[str, Any]:
    return {
        "call_control": sorted(capabilities.call_control),
        "inbound_audio": capabilities.inbound_audio,
        "outbound_audio": capabilities.outbound_audio,
        "interruptible_playback": capabilities.interruptible_playback,
        "concurrent_sessions": capabilities.concurrent_sessions,
        "modalities": capabilities.modalities.to_dict(),
    }


def default_transport_registry(enabled_kinds: set[TransportKind] | None = None) -> TransportRegistry:
    implemented = {"asterisk_audiosocket", "webrtc", "local"}
    enabled = implemented if enabled_kinds is None else enabled_kinds
    registry = TransportRegistry()
    for kind, capabilities in sorted(TRANSPORT_CAPABILITIES.items()):
        if kind in implemented:
            registry.register(
                StaticMediaTransport(kind, capabilities),
                enabled=kind in enabled,
            )
        else:
            registry.register_planned(
                kind,
                capabilities,
                enabled=kind in enabled,
                adapter_contract=TRANSPORT_ADAPTER_CONTRACTS.get(kind),
                unavailable_reason=TRANSPORT_UNAVAILABLE_REASONS.get(kind),
            )
    return registry


def transport_catalog(
    *,
    include_health: bool = False,
    enabled_kinds: set[TransportKind] | None = None,
) -> dict[str, Any]:
    return default_transport_registry(enabled_kinds=enabled_kinds).to_dict(include_health=include_health)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
