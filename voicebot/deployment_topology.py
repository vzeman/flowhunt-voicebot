from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import Settings


@dataclass(frozen=True)
class DeploymentRoleSpec:
    role: str
    worker_role: str | None
    local_process: str
    future_deployment: str
    queue: str | None
    readiness_checks: tuple[str, ...]
    startup_probe: str
    resource_profile: str
    compose_profile: str | None = None

    def to_dict(self, enabled: bool) -> dict[str, Any]:
        return {
            "role": self.role,
            "worker_role": self.worker_role,
            "enabled": enabled,
            "local_process": self.local_process,
            "future_deployment": self.future_deployment,
            "queue": self.queue,
            "readiness_checks": list(self.readiness_checks),
            "startup_probe": self.startup_probe,
            "resource_profile": self.resource_profile,
            "compose_profile": self.compose_profile,
        }


DEPLOYMENT_ROLES: tuple[DeploymentRoleSpec, ...] = (
    DeploymentRoleSpec(
        role="api_control_plane",
        worker_role="api",
        local_process="voicebot",
        future_deployment="voicebot-api",
        queue=None,
        readiness_checks=("providers", "event_catalog", "security_contract", "durable_storage", "drain"),
        startup_probe="/health/liveness",
        resource_profile="cpu-light memory-medium",
    ),
    DeploymentRoleSpec(
        role="sip_media_ingress",
        worker_role="media_ingress",
        local_process="asterisk + voicebot audiosocket",
        future_deployment="voicebot-sip-media",
        queue="voicebot.media",
        readiness_checks=("ami", "sip_media_plane", "realtime_audio", "drain"),
        startup_probe="/health/liveness",
        resource_profile="network-realtime cpu-medium memory-medium",
    ),
    DeploymentRoleSpec(
        role="webrtc_media_session",
        worker_role="media_ingress",
        local_process="voicebot webrtc manager",
        future_deployment="voicebot-webrtc-media",
        queue="voicebot.media",
        readiness_checks=("webrtc_media_plane", "realtime_audio", "drain"),
        startup_probe="/health/liveness",
        resource_profile="network-realtime cpu-medium memory-medium",
    ),
    DeploymentRoleSpec(
        role="session_orchestrator",
        worker_role="session_orchestrator",
        local_process="voicebot",
        future_deployment="voicebot-session-orchestrator",
        queue="voicebot.session",
        readiness_checks=("storage_contracts", "durable_storage", "drain"),
        startup_probe="/health/liveness",
        resource_profile="cpu-light memory-medium",
    ),
    DeploymentRoleSpec(
        role="stt_worker",
        worker_role="stt_worker",
        local_process="voicebot",
        future_deployment="voicebot-stt-worker",
        queue="voicebot.stt",
        readiness_checks=("providers", "realtime_audio", "durable_storage"),
        startup_probe="/health/liveness",
        resource_profile="cpu-or-gpu provider-egress memory-high",
    ),
    DeploymentRoleSpec(
        role="tts_worker",
        worker_role="tts_worker",
        local_process="voicebot",
        future_deployment="voicebot-tts-worker",
        queue="voicebot.tts",
        readiness_checks=("providers", "realtime_audio", "durable_storage"),
        startup_probe="/health/liveness",
        resource_profile="cpu-or-gpu provider-egress memory-high",
    ),
    DeploymentRoleSpec(
        role="communication_agent_worker",
        worker_role="agent_worker",
        local_process="openai-agent or anthropic-agent",
        future_deployment="voicebot-agent-worker",
        queue="voicebot.agent",
        readiness_checks=("providers", "durable_storage", "security_contract"),
        startup_probe="/agent/tasks/status",
        resource_profile="provider-egress cpu-light memory-medium",
    ),
    DeploymentRoleSpec(
        role="subagent_task_poller",
        worker_role="task_poller",
        local_process="voicebot lifespan task poller",
        future_deployment="voicebot-subagent-poller",
        queue="voicebot.external_tasks",
        readiness_checks=("storage_contracts", "durable_storage", "security_contract"),
        startup_probe="/subagent/tasks/lifecycle",
        resource_profile="provider-egress cpu-light memory-medium",
    ),
    DeploymentRoleSpec(
        role="post_call_worker",
        worker_role=None,
        local_process="voicebot",
        future_deployment="voicebot-post-call-worker",
        queue="voicebot.post_call",
        readiness_checks=("transcripts", "storage_contracts", "durable_storage", "security_contract"),
        startup_probe="/health/liveness",
        resource_profile="cpu-medium memory-medium object-storage",
    ),
)


def enabled_role_names(settings: Settings) -> tuple[str, ...]:
    configured = tuple(role.strip() for role in settings.runtime_roles if role.strip())
    if not configured or "all" in configured:
        return tuple(role.role for role in DEPLOYMENT_ROLES)
    known = {role.role for role in DEPLOYMENT_ROLES}
    return tuple(role for role in configured if role in known)


def unknown_role_names(settings: Settings) -> tuple[str, ...]:
    configured = tuple(role.strip() for role in settings.runtime_roles if role.strip())
    if not configured or "all" in configured:
        return ()
    known = {role.role for role in DEPLOYMENT_ROLES}
    return tuple(role for role in configured if role not in known)


def deployment_topology_payload(settings: Settings) -> dict[str, Any]:
    enabled = set(enabled_role_names(settings))
    unknown = unknown_role_names(settings)
    return {
        "mode": "all_in_one" if not unknown and len(enabled) == len(DEPLOYMENT_ROLES) else "role_filtered",
        "configured_roles": list(settings.runtime_roles),
        "unknown_roles": list(unknown),
        "roles": [role.to_dict(role.role in enabled) for role in DEPLOYMENT_ROLES],
        "local_docker": {
            "default": "single voicebot service plus asterisk and one communication-agent service",
            "split_testing": "run multiple voicebot containers with different VOICEBOT_RUNTIME_ROLES values against shared stores",
            "compose_profiles": ["anthropic"],
        },
        "future_kubernetes": {
            "manifests_included": False,
            "required_primitives": [
                "per-role Deployments",
                "role-specific readiness/liveness/startup probes",
                "PodDisruptionBudgets for media and worker roles",
                "HPA inputs from /scaling/signals",
                "workspace-scoped secret injection",
                "safe DB/queue migration hooks",
            ],
        },
    }


def role_readiness_payload(settings: Settings, readiness: dict[str, Any]) -> dict[str, Any]:
    enabled = set(enabled_role_names(settings))
    checks = readiness.get("checks") if isinstance(readiness.get("checks"), dict) else {}
    roles = []
    for role in DEPLOYMENT_ROLES:
        selected = role.role in enabled
        role_checks = {
            check_name: checks.get(check_name, {"ok": False, "message": "readiness check is not available"})
            for check_name in role.readiness_checks
        }
        roles.append(
            {
                **role.to_dict(selected),
                "ok": selected and all(bool(check.get("ok")) for check in role_checks.values()),
                "checks": role_checks,
            }
        )
    return {
        "ok": all(role["ok"] for role in roles if role["enabled"]),
        "configured_roles": list(settings.runtime_roles),
        "unknown_roles": list(unknown_role_names(settings)),
        "roles": roles,
    }
