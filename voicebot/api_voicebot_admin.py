from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter, HTTPException

from .api_models import (
    PublicVoicebotRoutePatchRequest,
    PublicVoicebotRouteRequest,
    VoicebotAdminPatchRequest,
    VoicebotAdminRequest,
    VoicebotChannelPatchRequest,
    VoicebotChannelRequest,
)
from .provider_catalog import _agent_capabilities, _stt_capabilities, _tts_capabilities
from .provider_config import (
    provider_selection_plan,
    selection_plan_to_dict,
    validate_provider_config,
    validation_issue_to_dict,
)
from .workspace_model import PublicVoicebotRoute, VoicebotChannelBinding, VoicebotDefinition


@dataclass(frozen=True)
class VoicebotAdminApiContext:
    voicebot_store: Any
    channel_resolver: Any
    public_route_store: Any
    provider_config_store: Any
    require_workspace_access: Callable[[str], None]


def create_voicebot_admin_router(context: VoicebotAdminApiContext) -> APIRouter:
    router = APIRouter()

    @router.get("/workspaces/{workspace_id}/voicebots")
    def list_workspace_voicebots(workspace_id: str) -> dict[str, Any]:
        context.require_workspace_access(workspace_id)
        return {
            "workspace_id": workspace_id,
            "voicebots": [voicebot.as_dict() for voicebot in context.voicebot_store.list(workspace_id)],
        }

    @router.post("/workspaces/{workspace_id}/voicebots")
    def create_workspace_voicebot(workspace_id: str, request: VoicebotAdminRequest) -> dict[str, Any]:
        context.require_workspace_access(workspace_id)
        try:
            voicebot = context.voicebot_store.create(
                VoicebotDefinition(
                    workspace_id=workspace_id,
                    voicebot_id=request.voicebot_id,
                    display_name=request.display_name,
                    enabled=request.enabled,
                    metadata=request.metadata,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"voicebot": voicebot.as_dict()}

    @router.get("/workspaces/{workspace_id}/voicebots/{voicebot_id}")
    def get_workspace_voicebot(workspace_id: str, voicebot_id: str) -> dict[str, Any]:
        context.require_workspace_access(workspace_id)
        voicebot = context.voicebot_store.get(workspace_id, voicebot_id)
        if voicebot is None:
            raise HTTPException(status_code=404, detail="Voicebot not found")
        return {"voicebot": voicebot.as_dict()}

    @router.patch("/workspaces/{workspace_id}/voicebots/{voicebot_id}")
    def patch_workspace_voicebot(
        workspace_id: str,
        voicebot_id: str,
        request: VoicebotAdminPatchRequest,
    ) -> dict[str, Any]:
        context.require_workspace_access(workspace_id)
        try:
            voicebot = context.voicebot_store.patch(
                workspace_id,
                voicebot_id,
                display_name=request.display_name,
                enabled=request.enabled,
                metadata=request.metadata,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="Voicebot not found") from None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"voicebot": voicebot.as_dict()}

    @router.delete("/workspaces/{workspace_id}/voicebots/{voicebot_id}")
    def delete_workspace_voicebot(workspace_id: str, voicebot_id: str) -> dict[str, Any]:
        context.require_workspace_access(workspace_id)
        voicebot = context.voicebot_store.delete(workspace_id, voicebot_id)
        if voicebot is None:
            raise HTTPException(status_code=404, detail="Voicebot not found")
        return {"voicebot": voicebot.as_dict(), "deleted": True}

    @router.get("/workspaces/{workspace_id}/voicebots/{voicebot_id}/channels")
    def list_voicebot_channels(workspace_id: str, voicebot_id: str) -> dict[str, Any]:
        context.require_workspace_access(workspace_id)
        return {
            "workspace_id": workspace_id,
            "voicebot_id": voicebot_id,
            "channels": [
                binding.as_dict()
                for binding in context.channel_resolver.bindings_for_voicebot(workspace_id, voicebot_id)
            ],
        }

    @router.post("/workspaces/{workspace_id}/voicebots/{voicebot_id}/channels")
    def create_voicebot_channel(
        workspace_id: str,
        voicebot_id: str,
        request: VoicebotChannelRequest,
    ) -> dict[str, Any]:
        context.require_workspace_access(workspace_id)
        try:
            binding = VoicebotChannelBinding(
                channel_id=request.channel_id,
                kind=request.kind,  # type: ignore[arg-type]
                workspace_id=workspace_id,
                voicebot_id=voicebot_id,
                external_id=request.external_id,
                enabled=request.enabled,
                metadata=request.metadata,
            )
            context.channel_resolver.register(binding)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"channel": binding.as_dict()}

    @router.get("/workspaces/{workspace_id}/voicebots/{voicebot_id}/channels/{channel_id}")
    def get_voicebot_channel(workspace_id: str, voicebot_id: str, channel_id: str) -> dict[str, Any]:
        context.require_workspace_access(workspace_id)
        binding = context.channel_resolver.get_channel(workspace_id, voicebot_id, channel_id)
        if binding is None:
            raise HTTPException(status_code=404, detail="Channel not found")
        return {"channel": binding.as_dict()}

    @router.patch("/workspaces/{workspace_id}/voicebots/{voicebot_id}/channels/{channel_id}")
    def patch_voicebot_channel(
        workspace_id: str,
        voicebot_id: str,
        channel_id: str,
        request: VoicebotChannelPatchRequest,
    ) -> dict[str, Any]:
        context.require_workspace_access(workspace_id)
        try:
            binding = context.channel_resolver.patch_channel(
                workspace_id,
                voicebot_id,
                channel_id,
                enabled=request.enabled,
                metadata=request.metadata,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="Channel not found") from None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"channel": binding.as_dict()}

    @router.delete("/workspaces/{workspace_id}/voicebots/{voicebot_id}/channels/{channel_id}")
    def delete_voicebot_channel(workspace_id: str, voicebot_id: str, channel_id: str) -> dict[str, Any]:
        context.require_workspace_access(workspace_id)
        binding = context.channel_resolver.unregister_voicebot_channel(workspace_id, voicebot_id, channel_id)
        if binding is None:
            raise HTTPException(status_code=404, detail="Channel not found")
        return {"channel": binding.as_dict(), "deleted": True}

    @router.get("/workspaces/{workspace_id}/voicebots/{voicebot_id}/public-routes")
    def list_public_voicebot_routes(workspace_id: str, voicebot_id: str) -> dict[str, Any]:
        context.require_workspace_access(workspace_id)
        return {
            "workspace_id": workspace_id,
            "voicebot_id": voicebot_id,
            "routes": [route.as_dict() for route in context.public_route_store.list(workspace_id, voicebot_id)],
        }

    @router.post("/workspaces/{workspace_id}/voicebots/{voicebot_id}/public-routes")
    def create_public_voicebot_route(
        workspace_id: str,
        voicebot_id: str,
        request: PublicVoicebotRouteRequest,
    ) -> dict[str, Any]:
        context.require_workspace_access(workspace_id)
        if context.channel_resolver.get_channel(workspace_id, voicebot_id, request.channel_id) is None:
            raise HTTPException(status_code=404, detail="Channel not found")
        try:
            route = context.public_route_store.save(
                PublicVoicebotRoute(
                    route_id=request.route_id,
                    workspace_id=workspace_id,
                    voicebot_id=voicebot_id,
                    channel_id=request.channel_id,
                    host=request.host,
                    path_prefix=request.path_prefix,
                    status=request.status,  # type: ignore[arg-type]
                    tls_mode=request.tls_mode,  # type: ignore[arg-type]
                    allowed_origins=tuple(request.allowed_origins),
                    metadata=request.metadata,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"route": route.as_dict()}

    @router.patch("/workspaces/{workspace_id}/voicebots/{voicebot_id}/public-routes/{route_id}")
    def patch_public_voicebot_route(
        workspace_id: str,
        voicebot_id: str,
        route_id: str,
        request: PublicVoicebotRoutePatchRequest,
    ) -> dict[str, Any]:
        context.require_workspace_access(workspace_id)
        existing = context.public_route_store.get(route_id, workspace_id)
        if existing is None or existing.voicebot_id != voicebot_id:
            raise HTTPException(status_code=404, detail="Public route not found")
        channel_id = request.channel_id or existing.channel_id
        if context.channel_resolver.get_channel(workspace_id, voicebot_id, channel_id) is None:
            raise HTTPException(status_code=404, detail="Channel not found")
        try:
            route = context.public_route_store.save(
                PublicVoicebotRoute(
                    route_id=existing.route_id,
                    workspace_id=workspace_id,
                    voicebot_id=voicebot_id,
                    channel_id=channel_id,
                    host=request.host or existing.host,
                    path_prefix=request.path_prefix or existing.path_prefix,
                    status=(request.status or existing.status),  # type: ignore[arg-type]
                    tls_mode=(request.tls_mode or existing.tls_mode),  # type: ignore[arg-type]
                    allowed_origins=tuple(
                        existing.allowed_origins if request.allowed_origins is None else request.allowed_origins
                    ),
                    metadata=existing.metadata if request.metadata is None else request.metadata,
                    created_at=existing.created_at,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"route": route.as_dict()}

    @router.delete("/workspaces/{workspace_id}/voicebots/{voicebot_id}/public-routes/{route_id}")
    def delete_public_voicebot_route(workspace_id: str, voicebot_id: str, route_id: str) -> dict[str, Any]:
        context.require_workspace_access(workspace_id)
        route = context.public_route_store.get(route_id, workspace_id)
        if route is None or route.voicebot_id != voicebot_id:
            raise HTTPException(status_code=404, detail="Public route not found")
        deleted = context.public_route_store.delete(route_id, workspace_id)
        assert deleted is not None
        return {"route": deleted.as_dict(), "deleted": True}

    @router.post("/workspaces/{workspace_id}/voicebots/{voicebot_id}/validate")
    def validate_voicebot_runtime(workspace_id: str, voicebot_id: str) -> dict[str, Any]:
        context.require_workspace_access(workspace_id)
        issues: list[dict[str, Any]] = []
        voicebot = context.voicebot_store.get(workspace_id, voicebot_id)
        channels = context.channel_resolver.bindings_for_voicebot(workspace_id, voicebot_id)
        config = context.provider_config_store.get(workspace_id, voicebot_id)
        selection_plan = None

        if voicebot is None:
            issues.append({"area": "voicebot", "message": "voicebot record is missing"})
        elif not voicebot.enabled:
            issues.append({"area": "voicebot", "message": "voicebot is disabled"})

        if not channels:
            issues.append({"area": "channel", "message": "voicebot has no channel bindings"})
        elif not any(channel.enabled for channel in channels):
            issues.append({"area": "channel", "message": "voicebot has no enabled channel bindings"})

        if config is None:
            issues.append({"area": "provider", "message": "provider config is missing"})
        else:
            descriptors = {
                "stt": _provider_catalog_descriptors("stt"),
                "tts": _provider_catalog_descriptors("tts"),
                "agent": _provider_catalog_descriptors("agent"),
            }
            issues.extend(
                {"area": "provider", **validation_issue_to_dict(issue)}
                for issue in validate_provider_config(config, descriptors)
            )
            if not any(issue["area"] == "provider" for issue in issues):
                selection_plan = selection_plan_to_dict(provider_selection_plan(config))

        return {
            "ok": len(issues) == 0,
            "workspace_id": workspace_id,
            "voicebot_id": voicebot_id,
            "channel_count": len(channels),
            "enabled_channel_count": len([channel for channel in channels if channel.enabled]),
            "selection_plan": selection_plan,
            "issues": issues,
        }

    return router


def _provider_catalog_descriptors(family: str):
    if family == "stt":
        return _stt_capabilities()
    if family == "tts":
        return _tts_capabilities()
    return _agent_capabilities()
