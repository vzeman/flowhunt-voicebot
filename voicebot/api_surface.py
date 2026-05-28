from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ApiArea = Literal[
    "admin",
    "runtime",
    "channel",
    "session",
    "transcript",
    "task",
    "provider",
    "transport",
    "testing",
    "internal",
]

ApiVisibility = Literal["public", "internal", "prototype"]
ApiScopeSource = Literal["path", "payload", "route_binding", "none"]


@dataclass(frozen=True)
class ApiEndpointSpec:
    method: str
    path: str
    area: ApiArea
    visibility: ApiVisibility
    workspace_scoped: bool = True
    scope_source: ApiScopeSource = "path"
    description: str = ""


FLOWHUNT_API_SURFACE: tuple[ApiEndpointSpec, ...] = (
    ApiEndpointSpec("GET", "/workspaces/{workspace_id}/voicebots", "admin", "public", description="List voicebots."),
    ApiEndpointSpec("POST", "/workspaces/{workspace_id}/voicebots", "admin", "public", description="Create voicebot."),
    ApiEndpointSpec("GET", "/workspaces/{workspace_id}/voicebots/{voicebot_id}", "admin", "public", description="Read voicebot."),
    ApiEndpointSpec("PATCH", "/workspaces/{workspace_id}/voicebots/{voicebot_id}", "admin", "public", description="Update voicebot."),
    ApiEndpointSpec("DELETE", "/workspaces/{workspace_id}/voicebots/{voicebot_id}", "admin", "public", description="Delete voicebot."),
    ApiEndpointSpec("GET", "/workspaces/{workspace_id}/voicebots/{voicebot_id}/channels", "channel", "public", description="List channels."),
    ApiEndpointSpec("POST", "/workspaces/{workspace_id}/voicebots/{voicebot_id}/channels", "channel", "public", description="Create channel."),
    ApiEndpointSpec("PATCH", "/workspaces/{workspace_id}/voicebots/{voicebot_id}/channels/{channel_id}", "channel", "public", description="Update channel."),
    ApiEndpointSpec("DELETE", "/workspaces/{workspace_id}/voicebots/{voicebot_id}/channels/{channel_id}", "channel", "public", description="Delete channel."),
    ApiEndpointSpec("POST", "/workspaces/{workspace_id}/voicebots/{voicebot_id}/validate", "admin", "public", description="Validate runtime config."),
    ApiEndpointSpec("PUT", "/workspaces/{workspace_id}/voicebots/{voicebot_id}/providers", "provider", "public", description="Save provider config."),
    ApiEndpointSpec("GET", "/workspaces/{workspace_id}/voicebots/{voicebot_id}/providers", "provider", "public", description="Read provider config."),
    ApiEndpointSpec("GET", "/workspaces/{workspace_id}/voicebots/{voicebot_id}/transports", "transport", "public", description="List transport capabilities."),
    ApiEndpointSpec("GET", "/workspaces/{workspace_id}/voicebots/{voicebot_id}/sessions", "session", "public", description="List sessions."),
    ApiEndpointSpec("GET", "/workspaces/{workspace_id}/voicebots/{voicebot_id}/sessions/{session_id}", "session", "public", description="Read session."),
    ApiEndpointSpec("GET", "/workspaces/{workspace_id}/voicebots/{voicebot_id}/sessions/{session_id}/timeline", "session", "public", description="Event timeline."),
    ApiEndpointSpec("GET", "/workspaces/{workspace_id}/voicebots/{voicebot_id}/sessions/{session_id}/transcript", "transcript", "public", description="Transcript."),
    ApiEndpointSpec("GET", "/workspaces/{workspace_id}/voicebots/{voicebot_id}/tasks", "task", "public", description="External task status."),
    ApiEndpointSpec("POST", "/runtime/webrtc/sessions", "runtime", "public", scope_source="payload", description="Create WebRTC runtime session."),
    ApiEndpointSpec("POST", "/runtime/sip-trunks/{trunk_id}/register", "runtime", "internal", scope_source="route_binding", description="Register SIP trunk runtime binding."),
    ApiEndpointSpec("GET", "/webrtc/test", "testing", "prototype", workspace_scoped=False, scope_source="none", description="Local browser test app."),
    ApiEndpointSpec("GET", "/agent/tasks", "internal", "internal", workspace_scoped=False, scope_source="none", description="Worker task lease API."),
)


def api_surface_by_area() -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for endpoint in FLOWHUNT_API_SURFACE:
        grouped.setdefault(endpoint.area, []).append(api_endpoint_to_dict(endpoint))
    return grouped


def api_surface_summary() -> dict:
    by_area: dict[str, int] = {}
    by_visibility: dict[str, int] = {}
    by_scope_source: dict[str, int] = {}

    for endpoint in FLOWHUNT_API_SURFACE:
        by_area[endpoint.area] = by_area.get(endpoint.area, 0) + 1
        by_visibility[endpoint.visibility] = by_visibility.get(endpoint.visibility, 0) + 1
        by_scope_source[endpoint.scope_source] = by_scope_source.get(endpoint.scope_source, 0) + 1

    return {
        "total": len(FLOWHUNT_API_SURFACE),
        "by_area": dict(sorted(by_area.items())),
        "by_visibility": dict(sorted(by_visibility.items())),
        "by_scope_source": dict(sorted(by_scope_source.items())),
    }


def prototype_endpoints() -> list[dict]:
    return [api_endpoint_to_dict(endpoint) for endpoint in FLOWHUNT_API_SURFACE if endpoint.visibility == "prototype"]


def public_endpoints_are_workspace_scoped() -> bool:
    return not api_scope_violations()


def api_surface_integrity_issues() -> list[dict]:
    issues: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for endpoint in FLOWHUNT_API_SURFACE:
        key = (endpoint.method.upper(), endpoint.path)
        if key in seen:
            issues.append({**api_endpoint_to_dict(endpoint), "issue": "duplicate method/path in API surface catalog"})
        seen.add(key)
        if not endpoint.description.strip():
            issues.append({**api_endpoint_to_dict(endpoint), "issue": "endpoint description is required"})
    return issues


def api_scope_violations() -> list[dict]:
    violations = []
    for endpoint in FLOWHUNT_API_SURFACE:
        if endpoint.visibility != "public":
            continue
        if not endpoint.workspace_scoped or endpoint.scope_source == "none":
            violations.append({**api_endpoint_to_dict(endpoint), "violation": "public endpoint is not workspace scoped"})
            continue
        if endpoint.scope_source == "path" and "/workspaces/{workspace_id}" not in endpoint.path:
            violations.append({**api_endpoint_to_dict(endpoint), "violation": "path-scoped endpoint lacks workspace_id path"})
    return violations


def api_endpoint_to_dict(endpoint: ApiEndpointSpec) -> dict:
    return {
        "method": endpoint.method,
        "path": endpoint.path,
        "area": endpoint.area,
        "visibility": endpoint.visibility,
        "workspace_scoped": endpoint.workspace_scoped,
        "scope_source": endpoint.scope_source,
        "description": endpoint.description,
    }
