from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import Settings
from .workspace_access import WorkspaceAccessPolicy


SENSITIVE_KEY_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "credential",
    "password",
    "secret",
    "token",
)


@dataclass(frozen=True)
class RetentionClass:
    name: str
    scope_fields: tuple[str, ...]
    default_days: int
    deletion_hook: str
    notes: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "scope_fields": list(self.scope_fields),
            "default_days": self.default_days,
            "deletion_hook": self.deletion_hook,
            "notes": self.notes,
        }


RETENTION_CLASSES: tuple[RetentionClass, ...] = (
    RetentionClass(
        name="events",
        scope_fields=("workspace_id", "voicebot_id", "session_id", "call_id"),
        default_days=30,
        deletion_hook="delete workspace/session event rows and compacted context summaries",
        notes="Operational audit events may be retained longer by FlowHunt policy.",
    ),
    RetentionClass(
        name="transcripts",
        scope_fields=("workspace_id", "voicebot_id", "session_id", "call_id"),
        default_days=30,
        deletion_hook="delete transcript rows and derived summaries",
        notes="Transcript reads must use workspace-scoped session routes.",
    ),
    RetentionClass(
        name="recordings",
        scope_fields=("workspace_id", "voicebot_id", "session_id", "artifact_id"),
        default_days=7,
        deletion_hook="delete object-storage recording/debug-audio artifacts and metadata",
        notes="Debug audio should stay disabled unless explicitly needed for support.",
    ),
    RetentionClass(
        name="cached_tts_audio",
        scope_fields=("workspace_id", "voicebot_id", "voice", "content_hash"),
        default_days=30,
        deletion_hook="delete cached generated audio blobs and index rows",
        notes="Cache keys must not contain raw caller text in production.",
    ),
    RetentionClass(
        name="subagent_tasks",
        scope_fields=("workspace_id", "voicebot_id", "session_id", "task_id"),
        default_days=30,
        deletion_hook="delete delegated task state, provider task refs, and late-result handoff rows",
        notes="Provider-side task retention follows the connected provider workspace policy.",
    ),
)


def redact_sensitive_data(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if is_sensitive_key(key_text):
                result[key_text] = {"configured": bool(item), "redacted": True}
            else:
                result[key_text] = redact_sensitive_data(item)
        return result
    if isinstance(value, list):
        return [redact_sensitive_data(item) for item in value]
    if isinstance(value, tuple):
        return [redact_sensitive_data(item) for item in value]
    return value


def is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in SENSITIVE_KEY_MARKERS)


def security_contract_payload(settings: Settings, workspace_policy: WorkspaceAccessPolicy) -> dict[str, Any]:
    production_mode = settings.deployment_mode.strip().lower() not in {"", "local", "development", "dev", "test"}
    return {
        "mode": "production_enforced" if production_mode else "local_permissive",
        "workspace_access": {
            "enabled": workspace_policy.enabled,
            "allowed_workspace_count": len(workspace_policy.allowed_workspace_ids),
            "mandatory_outside_local": True,
            "production_ready": (not production_mode) or workspace_policy.enabled,
        },
        "internal_api_auth": {
            "enabled": settings.internal_auth_enabled,
            "header": settings.internal_auth_header,
            "configured_key_count": len(settings.internal_api_keys),
            "mandatory_outside_local": True,
            "production_ready": (not production_mode) or (settings.internal_auth_enabled and bool(settings.internal_api_keys)),
        },
        "secret_handling": {
            "raw_secret_api_responses": False,
            "expected_storage": "workspace-scoped secret references",
            "redacted_key_markers": list(SENSITIVE_KEY_MARKERS),
            "local_env_secrets_allowed": not production_mode,
        },
        "audit": {
            "event_type": "security_audit",
            "required_for": [
                "call_control",
                "provider_config_change",
                "runtime_config_change",
                "channel_change",
                "sip_trunk_secret_change",
                "transcript_read",
                "retention_delete",
            ],
            "payload_policy": "redacted_recursive_json",
        },
        "pii_safe_logging": {
            "enabled": settings.pii_safe_logging_enabled,
            "transcript_text_in_diagnostics": False,
            "debug_audio_default_enabled": settings.debug_audio_capture,
        },
        "retention": {
            "classes": [item.to_dict() for item in RETENTION_CLASSES],
            "deletion_contract": "delete by workspace_id, then narrower voicebot/session/artifact scope when supplied",
        },
        "network_policy": {
            "asterisk": "voicebot media workers and SIP control workers only",
            "redis_or_queue": "runtime workers and lifecycle workers only",
            "database": "control-plane API and runtime workers with workspace-scoped queries",
            "provider_egress": "provider adapter workers only",
            "internal_api": "FlowHunt backend and authenticated worker identities only",
        },
        "webhook_input_validation": {
            "required": True,
            "minimum": ["content_type_json", "bounded_payload_size", "signature_or_internal_identity", "workspace_route_check"],
        },
    }


def security_contract_issues(settings: Settings, workspace_policy: WorkspaceAccessPolicy) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    production_mode = settings.deployment_mode.strip().lower() not in {"", "local", "development", "dev", "test"}
    if production_mode and not workspace_policy.enabled:
        issues.append(
            {
                "component": "workspace_access",
                "issue": "workspace authorization must be enabled outside local development",
            }
        )
    if production_mode and not settings.internal_auth_enabled:
        issues.append(
            {
                "component": "internal_api_auth",
                "issue": "internal API authentication must be enabled outside local development",
            }
        )
    if settings.internal_auth_enabled and not settings.internal_api_keys:
        issues.append(
            {
                "component": "internal_api_auth",
                "issue": "enabled internal API authentication requires at least one configured API key",
            }
        )
    if workspace_policy.enabled and not workspace_policy.allowed_workspace_ids:
        issues.append(
            {
                "component": "workspace_access",
                "issue": "enabled workspace authorization requires at least one allowed workspace id",
            }
        )
    for retention_class in RETENTION_CLASSES:
        if retention_class.default_days <= 0:
            issues.append({"component": "retention", "issue": f"{retention_class.name} retention must be positive"})
        if "workspace_id" not in retention_class.scope_fields:
            issues.append({"component": "retention", "issue": f"{retention_class.name} retention is not workspace scoped"})
    return issues
