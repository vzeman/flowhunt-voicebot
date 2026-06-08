from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, HTTPException

from .api_models import (
    VoicebotPromptConfigPatchRequest,
    VoicebotPromptConfigRequest,
    VoicebotRuntimeConfigRequest,
)
from .events import VoicebotEvent, event_to_dict
from .provider_catalog import _agent_capabilities, _stt_capabilities, _tts_capabilities
from .provider_config import (
    ProviderChoice,
    SecretReference,
    VoicebotProviderConfig,
    provider_selection_plan,
    selection_plan_to_dict,
    validate_provider_config,
    validation_issue_to_dict,
)
from .runtime_config import (
    VoicebotChannelConfig,
    VoicebotChatPromptConfig,
    VoicebotPromptConfig,
    VoicebotQuotaConfig,
    VoicebotRealtimeConfig,
    VoicebotRuntimeConfig,
    VoicebotSubagentConfig,
    runtime_config_to_dict,
)


@dataclass(frozen=True)
class VoicebotRuntimeConfigApiContext:
    runtime_config_store: Any
    prompt_config_store: Any
    provider_config_store: Any
    voicebot_store: Any
    events: Any
    require_workspace_access: Callable[[str], None]
    prompt_config_from_request: Callable[[Any], VoicebotPromptConfig]
    effective_prompt_config: Callable[[str | None, str | None], VoicebotPromptConfig]
    subagent_config_from_request: Callable[[Any], VoicebotSubagentConfig]
    append_security_audit: Callable[..., VoicebotEvent]
    broadcast: Callable[[VoicebotEvent], Awaitable[None]]


def create_voicebot_runtime_config_router(context: VoicebotRuntimeConfigApiContext) -> APIRouter:
    router = APIRouter()

    @router.get("/workspaces/{workspace_id}/voicebots/{voicebot_id}/runtime-config")
    def get_voicebot_runtime_config(workspace_id: str, voicebot_id: str) -> dict[str, Any]:
        context.require_workspace_access(workspace_id)
        config = context.runtime_config_store.get(workspace_id, voicebot_id)
        if config is None:
            raise HTTPException(status_code=404, detail="Runtime config not found")
        return {"config": runtime_config_to_dict(config)}

    @router.put("/workspaces/{workspace_id}/voicebots/{voicebot_id}/runtime-config")
    async def put_voicebot_runtime_config(
        workspace_id: str,
        voicebot_id: str,
        request: VoicebotRuntimeConfigRequest,
    ) -> dict[str, Any]:
        context.require_workspace_access(workspace_id)
        try:
            providers = VoicebotProviderConfig(
                workspace_id=workspace_id,
                voicebot_id=voicebot_id,
                stt=_provider_choice_from_request("stt", request.providers.stt, workspace_id),
                tts=_provider_choice_from_request("tts", request.providers.tts, workspace_id),
                agent=_provider_choice_from_request("agent", request.providers.agent, workspace_id),
            )
            config = VoicebotRuntimeConfig(
                workspace_id=workspace_id,
                voicebot_id=voicebot_id,
                config_version=1,
                providers=providers,
                prompts=context.prompt_config_from_request(request.prompts),
                realtime=VoicebotRealtimeConfig(**request.realtime.model_dump()),
                channels=VoicebotChannelConfig(**request.channels.model_dump()),
                quotas=VoicebotQuotaConfig(
                    max_concurrent_sessions=request.quotas.max_concurrent_sessions,
                    max_provider_inflight=request.quotas.max_provider_inflight,
                    enabled_actions=tuple(request.quotas.enabled_actions),
                ),
                subagents=context.subagent_config_from_request(request.subagents),
                enabled=request.enabled,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        descriptors = {
            "stt": _provider_catalog_descriptors("stt"),
            "tts": _provider_catalog_descriptors("tts"),
            "agent": _provider_catalog_descriptors("agent"),
        }
        issues = validate_provider_config(config.providers, descriptors)
        if issues:
            return {
                "ok": False,
                "config": runtime_config_to_dict(config),
                "validation": [validation_issue_to_dict(issue) for issue in issues],
            }
        saved = context.runtime_config_store.save(config)
        context.provider_config_store.save(saved.providers)
        event = context.events.append(
            "system",
            "runtime_config_updated",
            {
                "workspace_id": workspace_id,
                "voicebot_id": voicebot_id,
                "config_version": saved.config_version,
                "enabled": saved.enabled,
            },
        )
        audit_event = context.append_security_audit(
            workspace_id=workspace_id,
            voicebot_id=voicebot_id,
            action="runtime_config_change",
            actor="api",
            resource_type="runtime_config",
            resource_id=voicebot_id,
            outcome="saved",
            metadata=runtime_config_to_dict(saved),
        )
        await context.broadcast(event)
        await context.broadcast(audit_event)
        return {
            "ok": True,
            "config": runtime_config_to_dict(saved),
            "event": event_to_dict(event),
            "validation": [],
        }

    @router.get("/workspaces/{workspace_id}/voicebots/{voicebot_id}/prompts")
    def get_voicebot_prompts(workspace_id: str, voicebot_id: str) -> dict[str, Any]:
        context.require_workspace_access(workspace_id)
        if context.voicebot_store.get(workspace_id, voicebot_id) is None:
            raise HTTPException(status_code=404, detail="Voicebot not found")
        return _prompts_payload(context, workspace_id, voicebot_id)

    @router.put("/workspaces/{workspace_id}/voicebots/{voicebot_id}/prompts")
    async def put_voicebot_prompts(
        workspace_id: str,
        voicebot_id: str,
        request: VoicebotPromptConfigRequest,
    ) -> dict[str, Any]:
        context.require_workspace_access(workspace_id)
        if context.voicebot_store.get(workspace_id, voicebot_id) is None:
            raise HTTPException(status_code=404, detail="Voicebot not found")
        try:
            prompts = context.prompt_config_store.save(
                workspace_id,
                voicebot_id,
                context.prompt_config_from_request(request),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        event = context.events.append(
            "system",
            "voicebot_prompts_updated",
            {
                "workspace_id": workspace_id,
                "voicebot_id": voicebot_id,
                "fields": sorted(prompts.as_dict()),
            },
        )
        audit_event = context.append_security_audit(
            workspace_id=workspace_id,
            voicebot_id=voicebot_id,
            action="prompt_config_change",
            actor="api",
            resource_type="voicebot_prompts",
            resource_id=voicebot_id,
            outcome="saved",
            metadata=prompts.as_dict(),
        )
        await context.broadcast(event)
        await context.broadcast(audit_event)
        return {
            "ok": True,
            "workspace_id": workspace_id,
            "voicebot_id": voicebot_id,
            "prompts": prompts.as_dict(),
            "event": event_to_dict(event),
        }

    @router.patch("/workspaces/{workspace_id}/voicebots/{voicebot_id}/prompts")
    async def patch_voicebot_prompts(
        workspace_id: str,
        voicebot_id: str,
        request: VoicebotPromptConfigPatchRequest,
    ) -> dict[str, Any]:
        context.require_workspace_access(workspace_id)
        if context.voicebot_store.get(workspace_id, voicebot_id) is None:
            raise HTTPException(status_code=404, detail="Voicebot not found")
        current = context.effective_prompt_config(workspace_id, voicebot_id)
        payload = current.as_dict()
        updates = request.model_dump(exclude_none=True)
        if isinstance(updates.get("chat"), dict):
            payload["chat"] = {**payload.get("chat", {}), **updates.pop("chat")}
        payload.update(updates)
        try:
            chat_payload = payload.pop("chat", {}) or {}
            prompts = context.prompt_config_store.save(
                workspace_id,
                voicebot_id,
                VoicebotPromptConfig(**payload, chat=VoicebotChatPromptConfig(**chat_payload)),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        event = context.events.append(
            "system",
            "voicebot_prompts_updated",
            {
                "workspace_id": workspace_id,
                "voicebot_id": voicebot_id,
                "fields": sorted(updates),
            },
        )
        audit_event = context.append_security_audit(
            workspace_id=workspace_id,
            voicebot_id=voicebot_id,
            action="prompt_config_change",
            actor="api",
            resource_type="voicebot_prompts",
            resource_id=voicebot_id,
            outcome="saved",
            metadata=prompts.as_dict(),
        )
        await context.broadcast(event)
        await context.broadcast(audit_event)
        return {
            "ok": True,
            "workspace_id": workspace_id,
            "voicebot_id": voicebot_id,
            "prompts": prompts.as_dict(),
            "event": event_to_dict(event),
        }

    return router


def _prompts_payload(context: VoicebotRuntimeConfigApiContext, workspace_id: str, voicebot_id: str) -> dict[str, Any]:
    prompts = context.effective_prompt_config(workspace_id, voicebot_id)
    source = "default"
    if context.prompt_config_store.get(workspace_id, voicebot_id) is not None:
        source = "prompt_override"
    elif context.runtime_config_store.get(workspace_id, voicebot_id) is not None:
        source = "runtime_config"
    return {
        "workspace_id": workspace_id,
        "voicebot_id": voicebot_id,
        "source": source,
        "prompts": prompts.as_dict(),
    }


def _provider_choice_from_request(family: str, request, workspace_id: str) -> ProviderChoice:
    secret_ref = None
    if request.secret_ref is not None:
        secret_ref = SecretReference(
            name=request.secret_ref.name,
            workspace_id=request.secret_ref.workspace_id or workspace_id,
        )
    return ProviderChoice(
        family,  # type: ignore[arg-type]
        request.provider,
        model=request.model,
        voice=request.voice,
        secret_ref=secret_ref,
        fallback_provider=request.fallback_provider,
        config=request.config,
    )


def _provider_catalog_descriptors(family: str):
    if family == "stt":
        return _stt_capabilities()
    if family == "tts":
        return _tts_capabilities()
    return _agent_capabilities()
