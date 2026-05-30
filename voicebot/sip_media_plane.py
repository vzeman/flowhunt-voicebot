from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SipMediaPlaneDecision:
    topic: str
    local_docker: str
    kubernetes: str
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "local_docker": self.local_docker,
            "kubernetes": self.kubernetes,
            "rationale": self.rationale,
        }


SIP_MEDIA_PLANE_DECISIONS: tuple[SipMediaPlaneDecision, ...] = (
    SipMediaPlaneDecision(
        topic="sip_edge",
        local_docker="single_asterisk_registers_directly",
        kubernetes="sip_edge_layer_with_asterisk_media_workers",
        rationale="A SIP edge such as Kamailio/OpenSIPS can own provider-facing routing while Asterisk pods stay disposable media workers.",
    ),
    SipMediaPlaneDecision(
        topic="trunk_registration",
        local_docker="active_single_registration",
        kubernetes="provider_capability_driven_active_passive_or_active_active",
        rationale="Some SIP providers reject concurrent registrations; registration ownership must be explicit per workspace trunk.",
    ),
    SipMediaPlaneDecision(
        topic="active_call_failover",
        local_docker="mark_interrupted_on_media_loss",
        kubernetes="future_call_failover_not_rtp_continuity",
        rationale="The current AudioSocket/RTP path cannot guarantee seamless active media migration across pod loss.",
    ),
    SipMediaPlaneDecision(
        topic="draining",
        local_docker="disable_trunk_or_stop_accepting_new_audiosocket_sessions",
        kubernetes="remove_from_ready_routing_then_unregister_or_disable_trunks",
        rationale="Draining must reject new calls before pod termination while letting existing calls finish when possible.",
    ),
    SipMediaPlaneDecision(
        topic="call_control_routing",
        local_docker="local_ami",
        kubernetes="route_to_session_lease_owner",
        rationale="Hangup, transfer, DTMF, and playback controls must reach the media node that owns the session lease.",
    ),
)


def sip_media_plane_payload() -> dict[str, Any]:
    return {
        "architecture": {
            "local_docker": "single Asterisk container with direct PJSIP registration and AudioSocket handoff",
            "kubernetes_target": "SIP edge plus horizontally scaled Asterisk media workers and voicebot workers",
            "active_call_failover": "interrupted_not_migrated",
            "future_call_failover": "route only to ready media capacity",
        },
        "readiness_dimensions": {
            "api_ready": "voicebot API process accepts control-plane requests",
            "sip_registered": "trunk registration is healthy and owned by this media plane",
            "media_ready": "Asterisk/AudioSocket path can accept a routed call",
            "draining": "node is healthy for existing calls but should not receive new calls",
        },
        "routing": {
            "workspace_scope": ["workspace_id", "voicebot_id", "trunk_id"],
            "session_owner": "session lease owner receives media/control actions",
            "fallback_when_no_capacity": "reject_or_route_to_configured_overflow_target",
        },
        "decisions": [decision.to_dict() for decision in SIP_MEDIA_PLANE_DECISIONS],
        "local_development": {
            "supported": True,
            "keeps_existing_asterisk_path": True,
            "trunk_store": "local JSON plus rendered pjsip include",
        },
        "production_requirements": [
            "workspace-scoped trunk/channel config in FlowHunt storage",
            "provider-specific registration ownership policy",
            "SIP edge or provider-compatible active/passive registration controller",
            "media-node readiness that includes SIP registration and AudioSocket health",
            "drain workflow that removes ready routing before unregistering trunks",
            "call-control routing through session lease owner metadata",
        ],
    }


def sip_media_plane_issues(payload: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    contract = payload or sip_media_plane_payload()
    issues: list[dict[str, Any]] = []
    if not contract.get("architecture"):
        issues.append({"issue": "architecture is missing"})
    if not contract.get("decisions"):
        issues.append({"issue": "decisions are missing"})
    required_readiness = {"api_ready", "sip_registered", "media_ready", "draining"}
    readiness = set((contract.get("readiness_dimensions") or {}).keys())
    for missing in sorted(required_readiness - readiness):
        issues.append({"issue": "readiness dimension is missing", "dimension": missing})
    routing = contract.get("routing") or {}
    if routing.get("workspace_scope") != ["workspace_id", "voicebot_id", "trunk_id"]:
        issues.append({"issue": "workspace scoped trunk routing is not explicit"})
    if contract.get("architecture", {}).get("active_call_failover") != "interrupted_not_migrated":
        issues.append({"issue": "active call failover constraint is not explicit"})
    return issues
