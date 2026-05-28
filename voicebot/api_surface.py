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


@dataclass(frozen=True)
class ApiEndpointSpec:
    method: str
    path: str
    area: ApiArea
    visibility: ApiVisibility
    workspace_scoped: bool = True
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
    ApiEndpointSpec("POST", "/runtime/webrtc/sessions", "runtime", "public", description="Create WebRTC runtime session."),
    ApiEndpointSpec("POST", "/runtime/sip-trunks/{trunk_id}/register", "runtime", "internal", description="Register SIP trunk runtime binding."),
    ApiEndpointSpec("GET", "/webrtc/test", "testing", "prototype", workspace_scoped=False, description="Local browser test app."),
    ApiEndpointSpec("GET", "/agent/tasks", "internal", "internal", workspace_scoped=False, description="Worker task lease API."),
)


def api_surface_by_area() -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for endpoint in FLOWHUNT_API_SURFACE:
        grouped.setdefault(endpoint.area, []).append(api_endpoint_to_dict(endpoint))
    return grouped


def prototype_endpoints() -> list[dict]:
    return [api_endpoint_to_dict(endpoint) for endpoint in FLOWHUNT_API_SURFACE if endpoint.visibility == "prototype"]


def public_endpoints_are_workspace_scoped() -> bool:
    return all(endpoint.workspace_scoped for endpoint in FLOWHUNT_API_SURFACE if endpoint.visibility == "public")


def api_endpoint_to_dict(endpoint: ApiEndpointSpec) -> dict:
    return {
        "method": endpoint.method,
        "path": endpoint.path,
        "area": endpoint.area,
        "visibility": endpoint.visibility,
        "workspace_scoped": endpoint.workspace_scoped,
        "description": endpoint.description,
    }
