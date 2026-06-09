from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request

from .api_models import WebRTCOfferRequest
from .webrtc_media_plane import webrtc_media_plane_payload
from .workspace_model import PublicVoicebotRoute


@dataclass(frozen=True)
class WebRTCApiContext:
    webrtc: Any
    require_workspace_access: Callable[[str], None]
    resolve_public_voicebot_route: Callable[[Request], PublicVoicebotRoute | None]
    sanitized_public_visitor_metadata: Callable[[dict[str, Any]], dict[str, Any]]
    enforce_public_session_admission: Callable[[PublicVoicebotRoute, Request, WebRTCOfferRequest], None]
    effective_prompt_config: Callable[[str | None, str | None], Any]
    non_empty_str: Callable[[Any], str | None]


def create_webrtc_router(context: WebRTCApiContext) -> APIRouter:
    router = APIRouter()

    @router.get("/webrtc/sessions")
    def list_webrtc_sessions() -> dict[str, Any]:
        if context.webrtc is None:
            raise HTTPException(status_code=503, detail="WebRTC transport is not configured")
        return {"sessions": context.webrtc.snapshots()}

    @router.get("/webrtc/media-plane")
    def get_webrtc_media_plane() -> dict[str, Any]:
        return webrtc_media_plane_payload()

    @router.post("/webrtc/sessions")
    async def create_webrtc_session(request: WebRTCOfferRequest, http_request: Request) -> dict[str, Any]:
        if context.webrtc is None:
            raise HTTPException(status_code=503, detail="WebRTC transport is not configured")
        if request.type != "offer":
            raise HTTPException(status_code=400, detail="WebRTC session type must be offer")
        try:
            metadata = dict(request.metadata or {})
            route = context.resolve_public_voicebot_route(http_request)
            if route is not None:
                visitor_metadata = context.sanitized_public_visitor_metadata(metadata)
                metadata = {"visitor_metadata": visitor_metadata} if visitor_metadata else {}
                context.enforce_public_session_admission(route, http_request, request)
                metadata.update(route.event_data())
                metadata["public_route_resolved"] = True
            workspace_id = context.non_empty_str(metadata.get("workspace_id"))
            voicebot_id = context.non_empty_str(metadata.get("voicebot_id"))
            if workspace_id:
                context.require_workspace_access(workspace_id)
            if workspace_id and voicebot_id:
                metadata.setdefault(
                    "prompt_config",
                    context.effective_prompt_config(workspace_id, voicebot_id).as_dict(),
                )
            return await context.webrtc.create_session(request.sdp, request.type, metadata)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None

    @router.delete("/webrtc/sessions/{session_id}")
    async def delete_webrtc_session(session_id: str) -> dict[str, Any]:
        if context.webrtc is None:
            raise HTTPException(status_code=503, detail="WebRTC transport is not configured")
        closed = await context.webrtc.close_session(session_id)
        if not closed:
            raise HTTPException(status_code=404, detail=f"WebRTC session not found: {session_id}")
        return {"closed": True, "session_id": session_id}

    return router
