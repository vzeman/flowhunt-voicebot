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
    target_services: tuple[str, ...]
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
            "target_services": list(self.target_services),
            "queue": self.queue,
            "readiness_checks": list(self.readiness_checks),
            "startup_probe": self.startup_probe,
            "resource_profile": self.resource_profile,
            "compose_profile": self.compose_profile,
        }


@dataclass(frozen=True)
class KubernetesServiceSpec:
    service: str
    exposure: str
    roles: tuple[str, ...]
    ports: tuple[dict[str, Any], ...]
    ingress: str
    authentication: str
    openapi_spec: str | None
    readiness_endpoint: str
    notes: str

    def to_dict(self, enabled_roles: set[str]) -> dict[str, Any]:
        return {
            "service": self.service,
            "exposure": self.exposure,
            "roles": list(self.roles),
            "enabled": any(role in enabled_roles for role in self.roles),
            "ports": list(self.ports),
            "ingress": self.ingress,
            "authentication": self.authentication,
            "openapi_spec": self.openapi_spec,
            "readiness_endpoint": self.readiness_endpoint,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class IngressBoundarySpec:
    name: str
    exposure: str
    ingress_class: str
    hosts_paths: str
    forwards_to: str
    tls: str
    authentication: str
    allowed_route_audiences: tuple[str, ...]
    forbidden_surfaces: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "exposure": self.exposure,
            "ingress_class": self.ingress_class,
            "hosts_paths": self.hosts_paths,
            "forwards_to": self.forwards_to,
            "tls": self.tls,
            "authentication": self.authentication,
            "allowed_route_audiences": list(self.allowed_route_audiences),
            "forbidden_surfaces": list(self.forbidden_surfaces),
        }


DEPLOYMENT_ROLES: tuple[DeploymentRoleSpec, ...] = (
    DeploymentRoleSpec(
        role="api_control_plane",
        worker_role="api",
        local_process="voicebot",
        future_deployment="voicebot-internal-api",
        target_services=("voicebot-public-api", "voicebot-internal-api", "voicebot-dashboard"),
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
        target_services=("voicebot-sip-media",),
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
        target_services=("voicebot-webrtc-media",),
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
        target_services=("voicebot-agent-workers",),
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
        target_services=("voicebot-agent-workers",),
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
        target_services=("voicebot-agent-workers",),
        queue="voicebot.tts",
        readiness_checks=("providers", "realtime_audio", "durable_storage"),
        startup_probe="/health/liveness",
        resource_profile="cpu-or-gpu provider-egress memory-high",
    ),
    DeploymentRoleSpec(
        role="communication_agent_worker",
        worker_role="agent_worker",
        local_process="openai-agent or anthropic-agent",
        future_deployment="voicebot-agent-workers",
        target_services=("voicebot-agent-workers",),
        queue="voicebot.agent",
        readiness_checks=("providers", "durable_storage", "security_contract"),
        startup_probe="/agent/tasks/status",
        resource_profile="provider-egress cpu-light memory-medium",
    ),
    DeploymentRoleSpec(
        role="subagent_task_poller",
        worker_role="task_poller",
        local_process="voicebot lifespan task poller",
        future_deployment="voicebot-task-pollers",
        target_services=("voicebot-task-pollers",),
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
        target_services=("voicebot-agent-workers",),
        queue="voicebot.post_call",
        readiness_checks=("transcripts", "storage_contracts", "durable_storage", "security_contract"),
        startup_probe="/health/liveness",
        resource_profile="cpu-medium memory-medium object-storage",
    ),
)


KUBERNETES_SERVICES: tuple[KubernetesServiceSpec, ...] = (
    KubernetesServiceSpec(
        service="voicebot-public-api",
        exposure="internet",
        roles=("api_control_plane", "webrtc_media_session"),
        ports=({"name": "http", "port": 8080, "protocol": "TCP", "purpose": "public OpenAPI and WebRTC signaling"},),
        ingress="public-web",
        authentication="none for caller connection endpoints; admission uses public route, origin, rate, and capacity checks",
        openapi_spec="/openapi/public.json",
        readiness_endpoint="/health/readiness/roles",
        notes="Only public route-audience endpoints may be exposed.",
    ),
    KubernetesServiceSpec(
        service="voicebot-internal-api",
        exposure="private-cluster",
        roles=("api_control_plane",),
        ports=({"name": "http", "port": 8080, "protocol": "TCP", "purpose": "FlowHunt backend and worker control plane"},),
        ingress="internal-private",
        authentication="required internal API key or future service identity",
        openapi_spec="/openapi/internal.json",
        readiness_endpoint="/health/readiness/roles",
        notes="No internet route may forward to this service.",
    ),
    KubernetesServiceSpec(
        service="voicebot-dashboard",
        exposure="private-network",
        roles=("api_control_plane",),
        ports=({"name": "http", "port": 8080, "protocol": "TCP", "purpose": "admin dashboard and WebRTC test UI"},),
        ingress="dashboard-private",
        authentication="required internal auth now; future FlowHunt SSO/RBAC",
        openapi_spec=None,
        readiness_endpoint="/health/readiness/roles",
        notes="Dashboard stays private and is never part of public voicebot inference.",
    ),
    KubernetesServiceSpec(
        service="voicebot-webrtc-media",
        exposure="internet-media",
        roles=("webrtc_media_session",),
        ports=(
            {"name": "http-signaling", "port": 8080, "protocol": "TCP", "purpose": "SDP offer/answer signaling"},
            {"name": "rtp-udp", "port": "ephemeral", "protocol": "UDP", "purpose": "WebRTC ICE media candidates"},
        ),
        ingress="public-web plus TURN/STUN media edge",
        authentication="public route admission before session allocation",
        openapi_spec=None,
        readiness_endpoint="/health/readiness/roles",
        notes="Public browsers should prefer managed TURN for NAT traversal and stable firewall policy.",
    ),
    KubernetesServiceSpec(
        service="voicebot-sip-media",
        exposure="sip-provider-edge",
        roles=("sip_media_ingress",),
        ports=(
            {"name": "sip-udp", "port": 5060, "protocol": "UDP", "purpose": "SIP signaling"},
            {"name": "rtp-udp", "port": "10000-10100", "protocol": "UDP", "purpose": "RTP media"},
            {"name": "audiosocket", "port": 9019, "protocol": "TCP", "purpose": "Asterisk to voicebot audio bridge"},
        ),
        ingress="SIP load balancer or provider peering",
        authentication="SIP trunk credentials plus workspace route binding",
        openapi_spec=None,
        readiness_endpoint="/health/readiness/roles",
        notes="SIP/RTP exposure is separated from HTTP API ingress.",
    ),
    KubernetesServiceSpec(
        service="voicebot-agent-workers",
        exposure="private-cluster",
        roles=("session_orchestrator", "stt_worker", "tts_worker", "communication_agent_worker", "post_call_worker"),
        ports=({"name": "worker-http", "port": 8080, "protocol": "TCP", "purpose": "worker health and internal task APIs"},),
        ingress="none",
        authentication="internal API key or future workload identity",
        openapi_spec=None,
        readiness_endpoint="/health/readiness/roles",
        notes="Consumes queue work and calls providers; it is not reachable from callers.",
    ),
    KubernetesServiceSpec(
        service="voicebot-task-pollers",
        exposure="private-cluster",
        roles=("subagent_task_poller",),
        ports=({"name": "worker-http", "port": 8080, "protocol": "TCP", "purpose": "poller health and internal task lifecycle"},),
        ingress="none",
        authentication="internal API key or future workload identity",
        openapi_spec=None,
        readiness_endpoint="/health/readiness/roles",
        notes="Polls delegated subagent providers and publishes completed results.",
    ),
)


INGRESS_BOUNDARIES: tuple[IngressBoundarySpec, ...] = (
    IngressBoundarySpec(
        name="public-web",
        exposure="internet",
        ingress_class="public HTTPS ingress with managed/custom TLS",
        hosts_paths="workspace voicebot custom hosts and path prefixes from PublicVoicebotRoute",
        forwards_to="voicebot-public-api",
        tls="required",
        authentication="unauthenticated caller endpoints only",
        allowed_route_audiences=("public",),
        forbidden_surfaces=("internal OpenAPI", "dashboard", "events", "task queues", "diagnostics", "config"),
    ),
    IngressBoundarySpec(
        name="internal-private",
        exposure="private-cluster-or-vpn",
        ingress_class="private ingress or ClusterIP service",
        hosts_paths="FlowHunt backend and worker service names",
        forwards_to="voicebot-internal-api",
        tls="cluster policy",
        authentication="internal API key required",
        allowed_route_audiences=("internal",),
        forbidden_surfaces=("public anonymous browser traffic",),
    ),
    IngressBoundarySpec(
        name="dashboard-private",
        exposure="private-network",
        ingress_class="private ingress behind FlowHunt login",
        hosts_paths="admin-only dashboard host/path",
        forwards_to="voicebot-dashboard",
        tls="required",
        authentication="internal auth now; future SSO/RBAC",
        allowed_route_audiences=("local_dev", "internal"),
        forbidden_surfaces=("internet anonymous access",),
    ),
    IngressBoundarySpec(
        name="sip-provider-edge",
        exposure="provider-network",
        ingress_class="UDP load balancer or direct SIP provider peering",
        hosts_paths="SIP trunk registrations and RTP port ranges",
        forwards_to="voicebot-sip-media",
        tls="SIP/TLS optional by trunk capability; RTP/SRTP policy per provider",
        authentication="SIP credentials and channel routing admission",
        allowed_route_audiences=("sip-media",),
        forbidden_surfaces=("HTTP APIs", "dashboard", "OpenAPI"),
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
        "target_services": [service.to_dict(enabled) for service in KUBERNETES_SERVICES],
        "ingress_boundaries": [boundary.to_dict() for boundary in INGRESS_BOUNDARIES],
        "port_matrix": deployment_port_matrix(),
        "webrtc_ice": {
            "local_docker": "STUN-only is acceptable for local browser tests.",
            "production": "Use managed TURN/STUN and expose media through the voicebot-webrtc-media edge; do not rely on pod IP candidates.",
            "public_bootstrap_field": "ice_servers",
        },
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


def deployment_port_matrix() -> list[dict[str, Any]]:
    ports: list[dict[str, Any]] = []
    for service in KUBERNETES_SERVICES:
        for port in service.ports:
            ports.append(
                {
                    "service": service.service,
                    "exposure": service.exposure,
                    **port,
                }
            )
    return ports


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
        "routing": routing_readiness_payload(roles),
        "roles": roles,
    }


def routing_readiness_payload(role_payloads: list[dict[str, Any]]) -> dict[str, Any]:
    role_by_name = {str(role["role"]): role for role in role_payloads}

    def ready(*role_names: str) -> bool:
        selected = [role_by_name.get(name, {}) for name in role_names]
        return bool(selected) and all(bool(role.get("enabled")) and bool(role.get("ok")) for role in selected)

    return {
        "public_http_webrtc": {
            "safe": ready("api_control_plane", "webrtc_media_session"),
            "requires": ["api_control_plane", "webrtc_media_session"],
            "service": "voicebot-public-api",
        },
        "internal_api": {
            "safe": ready("api_control_plane"),
            "requires": ["api_control_plane"],
            "service": "voicebot-internal-api",
        },
        "sip_media": {
            "safe": ready("sip_media_ingress"),
            "requires": ["sip_media_ingress"],
            "service": "voicebot-sip-media",
        },
        "worker_queues": {
            "safe": ready(
                "session_orchestrator",
                "stt_worker",
                "tts_worker",
                "communication_agent_worker",
                "subagent_task_poller",
            ),
            "requires": [
                "session_orchestrator",
                "stt_worker",
                "tts_worker",
                "communication_agent_worker",
                "subagent_task_poller",
            ],
            "service": "voicebot-agent-workers",
        },
    }
