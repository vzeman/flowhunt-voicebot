from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

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


@dataclass(frozen=True)
class MediaSessionDescriptor:
    call_id: str
    transport: TransportKind
    route: CallRoute = field(default_factory=CallRoute)
    capabilities: TransportCapabilities = field(default_factory=TransportCapabilities)
    sample_rate: int = 8_000
    metadata: dict[str, Any] = field(default_factory=dict)

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


@runtime_checkable
class MediaTransport(Protocol):
    kind: TransportKind
    capabilities: TransportCapabilities

    def describe_session(self, call_id: str, metadata: dict[str, Any] | None = None) -> MediaSessionDescriptor:
        ...

    def execute_call_control(self, request: CallControlRequest) -> CallControlResult:
        ...


class StaticMediaTransport:
    def __init__(
        self,
        kind: TransportKind,
        capabilities: TransportCapabilities,
        *,
        sample_rate: int = 8_000,
    ) -> None:
        self.kind = kind
        self.capabilities = capabilities
        self.sample_rate = sample_rate

    def describe_session(self, call_id: str, metadata: dict[str, Any] | None = None) -> MediaSessionDescriptor:
        route = CallRoute.from_metadata(metadata)
        return MediaSessionDescriptor(
            call_id=call_id,
            transport=self.kind,
            route=route,
            capabilities=self.capabilities,
            sample_rate=self.sample_rate,
        )

    def execute_call_control(self, request: CallControlRequest) -> CallControlResult:
        if not self.capabilities.supports(request.action):
            return CallControlResult.unsupported(request, self.kind)
        return CallControlResult(
            call_id=request.call_id,
            action=request.action,
            ok=True,
            data={"transport": self.kind, **request.data},
        )


def transport_capabilities_to_dict(capabilities: TransportCapabilities) -> dict[str, Any]:
    return {
        "call_control": sorted(capabilities.call_control),
        "inbound_audio": capabilities.inbound_audio,
        "outbound_audio": capabilities.outbound_audio,
        "interruptible_playback": capabilities.interruptible_playback,
        "concurrent_sessions": capabilities.concurrent_sessions,
        "modalities": capabilities.modalities.to_dict(),
    }


def transport_catalog() -> dict[str, Any]:
    return {
        "transports": {
            kind: {
                "kind": kind,
                "capabilities": transport_capabilities_to_dict(capabilities),
                "implemented": kind in {"asterisk_audiosocket", "webrtc", "local"},
            }
            for kind, capabilities in sorted(TRANSPORT_CAPABILITIES.items())
        }
    }


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
