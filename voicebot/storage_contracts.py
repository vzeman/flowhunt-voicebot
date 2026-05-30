from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class StorageContract:
    name: str
    purpose: str
    local_providers: tuple[str, ...]
    production_backends: tuple[str, ...]
    required_scope_fields: tuple[str, ...]
    idempotency_fields: tuple[str, ...]
    consistency: str
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "purpose": self.purpose,
            "local_providers": list(self.local_providers),
            "production_backends": list(self.production_backends),
            "required_scope_fields": list(self.required_scope_fields),
            "idempotency_fields": list(self.idempotency_fields),
            "consistency": self.consistency,
            "notes": self.notes,
        }


LOCAL_JSON_PROVIDERS = ("json", "memory")


STORAGE_CONTRACTS: tuple[StorageContract, ...] = (
    StorageContract(
        name="events",
        purpose="Session timeline, metrics, call-control audit, provider telemetry, and agent/task events.",
        local_providers=LOCAL_JSON_PROVIDERS,
        production_backends=("flowhunt_db", "append_only_event_log"),
        required_scope_fields=("workspace_id", "voicebot_id", "session_id", "call_id"),
        idempotency_fields=("event_id",),
        consistency="append-only; event ids are monotonic per store provider",
        notes="Production queries must always be workspace-scoped and should support cursor pagination.",
    ),
    StorageContract(
        name="transcripts",
        purpose="Per-session transcript and post-call audit surface.",
        local_providers=("json",),
        production_backends=("flowhunt_db", "object_storage"),
        required_scope_fields=("workspace_id", "voicebot_id", "session_id", "call_id"),
        idempotency_fields=("event_id",),
        consistency="append-only per session; transcript rows derive from events",
        notes="Object storage is suitable for large recordings/debug audio; transcript indexes belong in DB.",
    ),
    StorageContract(
        name="voicebot_sessions",
        purpose="Workspace/voicebot session records and routed channel metadata.",
        local_providers=LOCAL_JSON_PROVIDERS,
        production_backends=("flowhunt_db",),
        required_scope_fields=("workspace_id", "voicebot_id", "session_id"),
        idempotency_fields=("session_id",),
        consistency="upsert by session_id; first route owns immutable workspace/voicebot scope",
    ),
    StorageContract(
        name="session_leases",
        purpose="Active media/session ownership and recovery handoff.",
        local_providers=LOCAL_JSON_PROVIDERS,
        production_backends=("redis", "lease_capable_kv"),
        required_scope_fields=("workspace_id", "voicebot_id", "session_id", "owner", "expires_at"),
        idempotency_fields=("workspace_id", "voicebot_id", "session_id"),
        consistency="atomic acquire/renew/release with TTL",
        notes="Used for pod ownership; active media is interrupted if ownership is lost.",
    ),
    StorageContract(
        name="agent_tasks",
        purpose="Communication-agent pending/claimed/responded task state.",
        local_providers=LOCAL_JSON_PROVIDERS,
        production_backends=("redis", "flowhunt_db"),
        required_scope_fields=("workspace_id", "voicebot_id", "session_id", "event_id"),
        idempotency_fields=("event_id",),
        consistency="claim with TTL; responded ids are idempotent acknowledgements",
    ),
    StorageContract(
        name="worker_queue",
        purpose="Durable internal work handoff for STT, TTS, agent, subagent, and post-call workers.",
        local_providers=LOCAL_JSON_PROVIDERS,
        production_backends=("redis_streams", "nats_jetstream", "rabbitmq", "flowhunt_queue"),
        required_scope_fields=("workspace_id", "voicebot_id", "session_id", "queue", "item_id"),
        idempotency_fields=("idempotency_key", "item_id"),
        consistency="claim/ack/release/retry with TTL and dead-letter visibility",
    ),
    StorageContract(
        name="worker_registry",
        purpose="Worker heartbeats, role capacity, drain state, and workspace/voicebot affinity.",
        local_providers=LOCAL_JSON_PROVIDERS,
        production_backends=("redis", "flowhunt_db"),
        required_scope_fields=("worker_id", "role"),
        idempotency_fields=("worker_id",),
        consistency="heartbeat TTL; expired workers are ignored",
    ),
    StorageContract(
        name="call_states",
        purpose="Active call/session snapshots for routing diagnostics and local restart visibility.",
        local_providers=LOCAL_JSON_PROVIDERS,
        production_backends=("redis", "flowhunt_db"),
        required_scope_fields=("workspace_id", "voicebot_id", "session_id", "call_id"),
        idempotency_fields=("call_id",),
        consistency="last-write-wins snapshot with updated_at timestamp",
    ),
    StorageContract(
        name="provider_config",
        purpose="Workspace/voicebot provider selections, model choices, prompts, and secret references.",
        local_providers=("memory",),
        production_backends=("flowhunt_db", "flowhunt_secret_store", "kubernetes_secret_projection"),
        required_scope_fields=("workspace_id", "voicebot_id", "config_version"),
        idempotency_fields=("workspace_id", "voicebot_id", "config_version"),
        consistency="versioned activation; active sessions keep the config version they started with",
        notes="Secrets are referenced, not returned raw.",
    ),
    StorageContract(
        name="sip_trunks",
        purpose="Workspace-scoped SIP trunk/channel bindings and registration ownership.",
        local_providers=("json",),
        production_backends=("flowhunt_db", "flowhunt_secret_store"),
        required_scope_fields=("workspace_id", "voicebot_id", "trunk_id"),
        idempotency_fields=("trunk_id",),
        consistency="upsert by trunk id; credentials stored only as secret references in production",
    ),
    StorageContract(
        name="subagent_tasks",
        purpose="Provider-neutral delegated work lifecycle, polling state, and late result handoff.",
        local_providers=LOCAL_JSON_PROVIDERS,
        production_backends=("flowhunt_db", "redis", "flowhunt_queue"),
        required_scope_fields=("workspace_id", "voicebot_id", "session_id", "task_id", "provider"),
        idempotency_fields=("workspace_id", "dedupe_key"),
        consistency="dedupe by workspace/dedupe key; terminal events emitted once",
    ),
    StorageContract(
        name="audio_artifacts",
        purpose="Recordings, debug audio, generated TTS audio, and reusable TTS cache entries.",
        local_providers=("filesystem",),
        production_backends=("object_storage", "cdn_cache"),
        required_scope_fields=("workspace_id", "voicebot_id", "session_id", "artifact_id"),
        idempotency_fields=("artifact_id", "content_hash"),
        consistency="content-addressed where possible; metadata indexed in DB",
    ),
)


def storage_contracts() -> tuple[StorageContract, ...]:
    return STORAGE_CONTRACTS


def storage_contracts_payload() -> dict[str, Any]:
    return {
        "contracts": [contract.to_dict() for contract in STORAGE_CONTRACTS],
        "local_development": {
            "supported": True,
            "providers": sorted({provider for contract in STORAGE_CONTRACTS for provider in contract.local_providers}),
        },
        "production": {
            "ready_for_external_backends": False,
            "required_before_kubernetes": [
                "FlowHunt DB-backed durable stores",
                "Redis or equivalent lease-capable active state",
                "queue/stream provider for worker handoff",
                "object storage for recordings and generated audio",
                "workspace-scoped secret references",
            ],
        },
    }


def storage_contract_issues() -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for contract in STORAGE_CONTRACTS:
        if not contract.required_scope_fields:
            issues.append({"name": contract.name, "issue": "required_scope_fields is empty"})
        if not contract.idempotency_fields:
            issues.append({"name": contract.name, "issue": "idempotency_fields is empty"})
        if not contract.local_providers:
            issues.append({"name": contract.name, "issue": "local_providers is empty"})
        if not contract.production_backends:
            issues.append({"name": contract.name, "issue": "production_backends is empty"})
    return issues
