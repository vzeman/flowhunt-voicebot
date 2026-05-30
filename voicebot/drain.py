from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass
class DrainState:
    draining: bool = False
    reason: str = ""
    started_at: str | None = None

    def start(self, reason: str = "operator_requested") -> dict[str, Any]:
        self.draining = True
        self.reason = reason.strip() or "operator_requested"
        self.started_at = datetime.now(UTC).isoformat()
        return self.snapshot()

    def stop(self) -> dict[str, Any]:
        self.draining = False
        self.reason = ""
        self.started_at = None
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        return {
            "draining": self.draining,
            "reason": self.reason,
            "started_at": self.started_at,
            "readiness_accepts_new_sessions": not self.draining,
            "active_media_failover": "interrupted_not_migrated",
            "worker_claim_policy": "renew_or_release_before_shutdown_otherwise_expire",
            "session_lease_policy": "renew_while_active_release_on_shutdown_expire_on_pod_loss",
        }


def rollout_contract() -> dict[str, Any]:
    return {
        "readiness": "fails while draining or unable to accept new sessions",
        "liveness": "fails only for unrecoverable stuck runtime state, not provider slowness",
        "pre_stop": [
            "set drain state",
            "stop accepting new SIP/WebRTC sessions",
            "unregister or disable SIP trunk ownership when applicable",
            "let active sessions finish until termination grace expires",
            "mark remaining sessions interrupted",
            "release session leases and worker claims where possible",
        ],
        "kubernetes": {
            "pod_disruption_budget": "keep at least one ready media replica per active routing pool",
            "termination_grace_period_seconds": "size to call drain timeout",
            "readiness_gates": ["api_ready", "sip_registered", "media_ready", "not_draining"],
            "deployments": ["api", "media", "workers", "task_pollers"],
        },
        "failover_guarantee": {
            "future_calls": "route to ready capacity",
            "active_rtp_or_webrtc_media": "not transparently migrated; mark interrupted on owner loss",
            "background_work": "recover through durable queues, leases, and subagent task state",
            "late_results": "store for transcript/audit, never speak into closed calls",
        },
    }
