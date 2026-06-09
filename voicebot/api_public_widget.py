from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from .workspace_model import PublicVoicebotRoute


@dataclass(frozen=True)
class PublicWidgetApiContext:
    events: Any
    voicebot_store: Any
    settings: Any
    resolve_public_voicebot_route: Callable[[Request], PublicVoicebotRoute | None]
    widget_script: str
    widget_page: str


def create_public_widget_router(context: PublicWidgetApiContext) -> APIRouter:
    router = APIRouter()

    @router.get("/.well-known/flowhunt-voicebot")
    def public_voicebot_bootstrap(request: Request) -> dict[str, Any]:
        route = context.resolve_public_voicebot_route(request)
        if route is None:
            context.events.append(
                "system",
                "session_admission_decided",
                {
                    "transport": "webrtc",
                    "decision": "reject",
                    "reason": "public_route_not_found",
                    "host": request.headers.get("x-forwarded-host") or request.headers.get("host") or "",
                },
            )
            raise HTTPException(status_code=404, detail="Public voicebot route not found")
        voicebot = context.voicebot_store.get(route.workspace_id, route.voicebot_id)
        widget_config = caller_safe_widget_config(route, voicebot.display_name if voicebot else "")
        return {
            "route_id": route.route_id,
            "workspace_id": route.workspace_id,
            "voicebot_id": route.voicebot_id,
            "channel_id": route.channel_id,
            "display_name": voicebot.display_name if voicebot else "",
            "transport": "webrtc",
            "session_endpoint": "/webrtc/sessions",
            "widget_script": "/widget.js",
            "widget_page": "/widget",
            "widget": widget_config,
            "ice_servers": list(context.settings.webrtc_stun_urls),
            "modalities": {"input": ["audio"], "output": ["audio"]},
            "limits": {
                "sdp_max_bytes": context.settings.public_sdp_max_bytes,
                "rate_limit_per_minute": context.settings.public_session_rate_limit_per_minute,
                "max_concurrent_sessions": context.settings.public_voicebot_max_concurrent_sessions,
            },
        }

    @router.get("/widget.js")
    def public_widget_script() -> Response:
        return Response(
            content=context.widget_script,
            media_type="application/javascript; charset=utf-8",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    @router.get("/widget")
    def public_widget_page() -> HTMLResponse:
        return HTMLResponse(context.widget_page)

    return router


def caller_safe_widget_config(route: PublicVoicebotRoute, display_name: str) -> dict[str, Any]:
    metadata = route.metadata if isinstance(route.metadata, dict) else {}
    theme = metadata.get("theme") if isinstance(metadata.get("theme"), dict) else {}
    primary_color = str(theme.get("primary_color") or metadata.get("primary_color") or "#0969da")[:32]
    placement = str(theme.get("placement") or metadata.get("placement") or "bottom-right")[:32]
    launcher_label = str(metadata.get("launcher_label") or display_name or "Start voice call")[:80]
    return {
        "enabled": route.status == "active",
        "display_name": display_name,
        "launcher_label": launcher_label,
        "welcome_label": str(metadata.get("welcome_label") or "Voice call")[:80],
        "locale": str(metadata.get("locale") or "")[:32],
        "theme": {
            "primary_color": primary_color,
            "placement": placement,
        },
        "show_captions": bool(metadata.get("show_captions", False)),
        "visitor_metadata_max_bytes": 2048,
        "recording_visible_to_visitor": bool(metadata.get("recording_visible_to_visitor", False)),
    }
