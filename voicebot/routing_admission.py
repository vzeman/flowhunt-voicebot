from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .provider_config import ProviderConfigStore
from .runtime_config import VoicebotRuntimeConfigStore
from .scaling import WarmCapacityPolicy, admission_decision
from .session_leases import SessionLeaseStore
from .workspace_access import WorkspaceAccessPolicy
from .workspace_model import ChannelKind, ChannelResolver, VoicebotStore


@dataclass(frozen=True)
class IncomingSessionRequest:
    channel_kind: ChannelKind
    external_id: str
    session_id: str
    owner: str
    transport: str
    call_id: str | None = None
    acquire_lease: bool = True
    lease_ttl_seconds: float = 30.0
    max_concurrent_sessions: int = 100
    burst_sessions: int = 0

    def __post_init__(self) -> None:
        if not self.external_id.strip():
            raise ValueError("external_id is required")
        if not self.session_id.strip():
            raise ValueError("session_id is required")
        if not self.owner.strip():
            raise ValueError("owner is required")
        if not self.transport.strip():
            raise ValueError("transport is required")


def evaluate_incoming_session(
    request: IncomingSessionRequest,
    *,
    channel_resolver: ChannelResolver,
    voicebot_store: VoicebotStore,
    provider_config_store: ProviderConfigStore,
    runtime_config_store: VoicebotRuntimeConfigStore,
    workspace_access_policy: WorkspaceAccessPolicy,
    session_lease_store: SessionLeaseStore,
    active_session_snapshots: list[dict[str, Any]],
) -> dict[str, Any]:
    scope = channel_resolver.resolve(request.channel_kind, request.external_id)
    if scope is None:
        return _decision("reject", False, "channel_not_found", request)
    try:
        workspace_access_policy.require_workspace(scope.workspace_id)
    except PermissionError:
        return _decision("reject", False, "workspace_access_denied", request, scope=scope.event_data())

    voicebot = voicebot_store.get(scope.workspace_id, scope.voicebot_id)
    if voicebot is None:
        return _decision("reject", False, "voicebot_not_found", request, scope=scope.event_data())
    if not voicebot.enabled:
        return _decision("reject", False, "voicebot_disabled", request, scope=scope.event_data())

    runtime_config = runtime_config_store.get(scope.workspace_id, scope.voicebot_id)
    if runtime_config is not None and not runtime_config.enabled:
        return _decision("reject", False, "runtime_config_disabled", request, scope=scope.event_data())
    if runtime_config is None and provider_config_store.get(scope.workspace_id, scope.voicebot_id) is None:
        return _decision("reject", False, "provider_config_missing", request, scope=scope.event_data())

    capacity = admission_decision(
        active_session_snapshots=active_session_snapshots,
        workspace_id=scope.workspace_id,
        voicebot_id=scope.voicebot_id,
        policy=WarmCapacityPolicy(
            max_concurrent_sessions=request.max_concurrent_sessions,
            burst_sessions=request.burst_sessions,
        ),
    )
    if not capacity["allowed"]:
        return {
            **_decision(capacity["decision"], False, capacity["reason"], request, scope=scope.event_data()),
            "capacity": capacity,
            "fallback": fallback_for_transport(request.transport, capacity["reason"]),
        }

    lease = None
    if request.acquire_lease:
        lease = session_lease_store.acquire(
            scope.workspace_id,
            scope.voicebot_id,
            request.session_id,
            request.owner,
            request.lease_ttl_seconds,
            call_id=request.call_id or request.session_id,
            transport=request.transport,
            metadata={"channel_kind": request.channel_kind, "external_id": request.external_id},
        )
        if lease is None:
            return _decision("reject", False, "session_lease_unavailable", request, scope=scope.event_data())

    return {
        **_decision("accept", True, "accepted", request, scope=scope.event_data()),
        "capacity": capacity,
        "lease": lease.as_dict() if lease is not None else None,
        "fallback": None,
    }


def fallback_for_transport(transport: str, reason: str) -> dict[str, Any]:
    if transport in {"asterisk_audiosocket", "sip", "sip_trunk"}:
        return {
            "transport": transport,
            "kind": "sip_busy_or_unavailable",
            "reason": reason,
            "options": ["sip_486_busy_here", "sip_503_service_unavailable", "transfer_to_fallback_extension"],
        }
    if transport == "webrtc":
        return {
            "transport": transport,
            "kind": "http_error_before_sdp_answer",
            "status_code": 429,
            "reason": reason,
        }
    return {"transport": transport, "kind": "reject", "reason": reason}


def _decision(
    decision: str,
    allowed: bool,
    reason: str,
    request: IncomingSessionRequest,
    *,
    scope: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "allowed": allowed,
        "decision": decision,
        "reason": reason,
        "channel": {"kind": request.channel_kind, "external_id": request.external_id},
        "session_id": request.session_id,
        "transport": request.transport,
        **(scope or {}),
    }
