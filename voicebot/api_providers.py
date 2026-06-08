from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter, HTTPException

from .api_models import ProviderChoiceRequest, VoicebotProviderConfigRequest
from .provider_catalog import _agent_capabilities, _stt_capabilities, _tts_capabilities, provider_catalog
from .provider_config import (
    ProviderChoice,
    SecretReference,
    VoicebotProviderConfig,
    provider_config_to_dict,
    provider_selection_plan,
    selection_plan_to_dict,
    validate_provider_config,
    validation_issue_to_dict,
)


@dataclass(frozen=True)
class ProvidersApiContext:
    provider_config_store: Any
    require_workspace_access: Callable[[str], None]
    append_security_audit: Callable[..., Any]


def create_providers_router(context: ProvidersApiContext) -> APIRouter:
    router = APIRouter()

    @router.get("/providers")
    def providers() -> dict[str, Any]:
        return provider_catalog()

    @router.get("/workspaces/{workspace_id}/voicebots/{voicebot_id}/providers")
    def get_voicebot_provider_config(workspace_id: str, voicebot_id: str) -> dict[str, Any]:
        context.require_workspace_access(workspace_id)
        config = context.provider_config_store.get(workspace_id, voicebot_id)
        if config is None:
            raise HTTPException(status_code=404, detail="Provider config not found")
        return {
            "config": provider_config_to_dict(config),
            "selection_plan": selection_plan_to_dict(provider_selection_plan(config)),
            "validation": [],
        }

    @router.put("/workspaces/{workspace_id}/voicebots/{voicebot_id}/providers")
    def put_voicebot_provider_config(
        workspace_id: str,
        voicebot_id: str,
        request: VoicebotProviderConfigRequest,
    ) -> dict[str, Any]:
        context.require_workspace_access(workspace_id)
        try:
            config = VoicebotProviderConfig(
                workspace_id=workspace_id,
                voicebot_id=voicebot_id,
                stt=_provider_choice_from_request("stt", request.stt, workspace_id),
                tts=_provider_choice_from_request("tts", request.tts, workspace_id),
                agent=_provider_choice_from_request("agent", request.agent, workspace_id),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        descriptors = {
            "stt": _provider_catalog_descriptors("stt"),
            "tts": _provider_catalog_descriptors("tts"),
            "agent": _provider_catalog_descriptors("agent"),
        }
        issues = validate_provider_config(config, descriptors)
        if issues:
            return {
                "ok": False,
                "config": provider_config_to_dict(config),
                "validation": [validation_issue_to_dict(issue) for issue in issues],
            }
        saved = context.provider_config_store.save(config)
        context.append_security_audit(
            workspace_id=workspace_id,
            voicebot_id=voicebot_id,
            action="provider_config_change",
            actor="api",
            resource_type="provider_config",
            resource_id=voicebot_id,
            outcome="saved",
            metadata=provider_config_to_dict(saved),
        )
        return {
            "ok": True,
            "config": provider_config_to_dict(saved),
            "selection_plan": selection_plan_to_dict(provider_selection_plan(saved)),
            "validation": [],
        }

    return router


def _provider_choice_from_request(
    family: str,
    request: ProviderChoiceRequest,
    workspace_id: str,
) -> ProviderChoice:
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
