from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, HTTPException

from .api_models import RetentionDeleteRequest, SecurityAuditRequest
from .events import VoicebotEvent, event_to_dict
from .security_contract import security_contract_issues, security_contract_payload


@dataclass(frozen=True)
class SecurityApiContext:
    runtime_settings: Any
    workspace_access_policy: Any
    require_workspace_access: Callable[[str], None]
    append_security_audit: Callable[..., VoicebotEvent]
    broadcast: Callable[[VoicebotEvent], Awaitable[None]]


def create_security_router(context: SecurityApiContext) -> APIRouter:
    router = APIRouter()

    @router.get("/security/contract")
    def security_contract() -> dict[str, Any]:
        return {
            "contract": security_contract_payload(context.runtime_settings, context.workspace_access_policy),
            "issues": security_contract_issues(context.runtime_settings, context.workspace_access_policy),
        }

    @router.get("/workspaces/{workspace_id}/security/retention")
    def workspace_security_retention(workspace_id: str) -> dict[str, Any]:
        context.require_workspace_access(workspace_id)
        contract = security_contract_payload(context.runtime_settings, context.workspace_access_policy)
        return {"workspace_id": workspace_id, "retention": contract["retention"]}

    @router.post("/workspaces/{workspace_id}/security/audit")
    async def workspace_security_audit(workspace_id: str, request: SecurityAuditRequest) -> dict[str, Any]:
        context.require_workspace_access(workspace_id)
        event = context.append_security_audit(
            workspace_id=workspace_id,
            voicebot_id=request.voicebot_id,
            session_id=request.session_id,
            call_id=request.call_id,
            action=request.action,
            actor=request.actor,
            resource_type=request.resource_type,
            resource_id=request.resource_id,
            outcome=request.outcome,
            metadata=request.metadata,
        )
        await context.broadcast(event)
        return {"event": event_to_dict(event)}

    @router.post("/workspaces/{workspace_id}/security/retention/delete")
    async def workspace_retention_delete(workspace_id: str, request: RetentionDeleteRequest) -> dict[str, Any]:
        context.require_workspace_access(workspace_id)
        contract = security_contract_payload(context.runtime_settings, context.workspace_access_policy)
        known_classes = {item["name"]: item for item in contract["retention"]["classes"]}
        selected = request.classes or sorted(known_classes)
        unknown = [name for name in selected if name not in known_classes]
        if unknown:
            raise HTTPException(status_code=400, detail={"unknown_retention_classes": unknown})
        scope = {
            "workspace_id": workspace_id,
            "voicebot_id": request.voicebot_id,
            "session_id": request.session_id,
            "call_id": request.call_id,
            "artifact_id": request.artifact_id,
        }
        hooks = [
            {
                "class": name,
                "deletion_hook": known_classes[name]["deletion_hook"],
                "scope": {key: value for key, value in scope.items() if value},
                "dry_run": request.dry_run,
            }
            for name in selected
        ]
        event = context.append_security_audit(
            workspace_id=workspace_id,
            voicebot_id=request.voicebot_id,
            session_id=request.session_id,
            call_id=request.call_id,
            action="retention_delete",
            actor="dashboard_or_internal_api",
            resource_type="retention_scope",
            resource_id=request.artifact_id or request.session_id or request.voicebot_id or workspace_id,
            outcome="planned" if request.dry_run else "requested",
            metadata={"classes": selected, "scope": scope, "reason": request.reason, "dry_run": request.dry_run},
        )
        await context.broadcast(event)
        return {
            "workspace_id": workspace_id,
            "dry_run": request.dry_run,
            "hooks": hooks,
            "audit_event": event_to_dict(event),
        }

    return router
