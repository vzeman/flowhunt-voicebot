from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WebRTCMediaPlaneDecision:
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


WEBRTC_MEDIA_PLANE_DECISIONS: tuple[WebRTCMediaPlaneDecision, ...] = (
    WebRTCMediaPlaneDecision(
        topic="signaling",
        local_docker="voicebot_api_terminates_offer_answer",
        kubernetes="workspace_scoped_signaling_with_session_stickiness",
        rationale="The peer connection and media worker that accept the offer must remain addressable for lifecycle and cleanup.",
    ),
    WebRTCMediaPlaneDecision(
        topic="ice",
        local_docker="public_stun_or_empty_stun_list",
        kubernetes="managed_turn_with_workspace_scoped_secret_references",
        rationale="Production browsers need reliable NAT traversal and credentials must not be embedded in frontend code.",
    ),
    WebRTCMediaPlaneDecision(
        topic="admission",
        local_docker="best_effort_session_creation",
        kubernetes="capacity_check_before_peer_connection_allocation",
        rationale="Admission should reject or redirect before expensive WebRTC media/session resources are allocated.",
    ),
    WebRTCMediaPlaneDecision(
        topic="reconnect",
        local_docker="new_session_after_failed_peer_connection",
        kubernetes="reconnect_to_active_session_when_owner_alive_otherwise_mark_interrupted",
        rationale="ICE restarts can recover transient network changes, but pod loss has the same media continuity limit as SIP.",
    ),
    WebRTCMediaPlaneDecision(
        topic="quality_metrics",
        local_docker="connection_state_and_audio_pipeline_metrics",
        kubernetes="ice_state_packet_loss_jitter_rtt_audio_level_disconnect_reason",
        rationale="Autoscaling and incident response need transport-level quality signals, not only agent latency.",
    ),
)


def webrtc_media_plane_payload() -> dict[str, Any]:
    return {
        "architecture": {
            "local_docker": "self-hosted aiortc peer connection inside the voicebot API process",
            "kubernetes_target": "workspace-scoped signaling with sticky media workers and optional managed media-server integration",
            "managed_media_server_future_options": ["livekit_style", "daily_style", "managed_turn"],
            "active_session_failover": "reconnect_or_interrupted_not_transparent_media_migration",
        },
        "routing": {
            "workspace_scope": ["workspace_id", "voicebot_id", "channel_id"],
            "session_owner": "session lease owner handles peer connection lifecycle and media/control actions",
            "signaling_stickiness": "session_id routes to the owning media worker until closed or interrupted",
            "admission_control": "check workspace voicebot capacity before creating RTCPeerConnection",
        },
        "ice": {
            "stun": "configurable per environment",
            "turn": "required for production reliability",
            "secret_handling": "TURN credentials are referenced by workspace/voicebot secret refs and never returned raw",
        },
        "cleanup": {
            "failed_or_disconnected": "close peer connection, stop session, release lease, emit call_ended/session_interrupted as appropriate",
            "browser_reconnect": "client creates a new offer with previous session_id when supported",
        },
        "quality_metrics": ["ice_state", "connection_state", "packet_loss", "jitter", "rtt", "audio_level", "disconnect_reason"],
        "decisions": [decision.to_dict() for decision in WEBRTC_MEDIA_PLANE_DECISIONS],
        "local_development": {"browser_test_page": "/webrtc/test", "supported": True},
        "production_requirements": [
            "workspace-scoped WebRTC channel bindings",
            "sticky signaling route for active session owner",
            "TURN credentials from secret references",
            "admission control before RTCPeerConnection allocation",
            "quality metrics exported to observability timelines",
            "cleanup loop for stale peer connections and expired session leases",
        ],
    }


def webrtc_media_plane_issues(payload: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    contract = payload or webrtc_media_plane_payload()
    issues: list[dict[str, Any]] = []
    if contract.get("architecture", {}).get("active_session_failover") != "reconnect_or_interrupted_not_transparent_media_migration":
        issues.append({"issue": "active WebRTC failover boundary is not explicit"})
    if contract.get("routing", {}).get("workspace_scope") != ["workspace_id", "voicebot_id", "channel_id"]:
        issues.append({"issue": "workspace scoped WebRTC routing is not explicit"})
    ice = contract.get("ice") or {}
    if ice.get("turn") != "required for production reliability":
        issues.append({"issue": "TURN production requirement is missing"})
    required_metrics = {"ice_state", "connection_state", "packet_loss", "jitter", "rtt", "audio_level", "disconnect_reason"}
    metrics = set(contract.get("quality_metrics") or [])
    for missing in sorted(required_metrics - metrics):
        issues.append({"issue": "quality metric is missing", "metric": missing})
    return issues
