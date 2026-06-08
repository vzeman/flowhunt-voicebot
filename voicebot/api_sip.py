from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter, HTTPException

from .api_models import SipTrunkRequest
from .asterisk_control import ControlResult
from .events import VoicebotEvent
from .sip_media_plane import sip_media_plane_payload
from .sip_trunks import SipTrunk


@dataclass(frozen=True)
class SipApiContext:
    sip_trunks: Any
    asterisk: Any
    append_security_audit: Callable[..., VoicebotEvent]


def create_sip_router(context: SipApiContext) -> APIRouter:
    router = APIRouter()

    @router.get("/sip-trunks")
    def list_sip_trunks() -> dict[str, Any]:
        if context.sip_trunks is None:
            raise HTTPException(status_code=503, detail="SIP trunk registry is not configured")
        return {
            "trunks": [trunk.redacted_dict() for trunk in context.sip_trunks.list()],
            "registrations": _control_result_dict(
                _safe_asterisk_action(context, lambda: context.asterisk.show_registrations())
            ),
        }

    @router.get("/sip/media-plane")
    def get_sip_media_plane() -> dict[str, Any]:
        return sip_media_plane_payload()

    @router.post("/sip-trunks")
    def upsert_sip_trunk(request: SipTrunkRequest) -> dict[str, Any]:
        if context.sip_trunks is None:
            raise HTTPException(status_code=503, detail="SIP trunk registry is not configured")
        try:
            trunk = SipTrunk(
                trunk_id=request.trunk_id,
                host=request.host,
                user=request.user,
                password=request.password,
                auth_user=request.auth_user,
                contact_user=request.contact_user,
                from_user=request.from_user,
                display_name=request.display_name,
                enabled=request.enabled,
                codecs=tuple(request.codecs),
                expiration=request.expiration,
                retry_interval=request.retry_interval,
                forbidden_retry_interval=request.forbidden_retry_interval,
            )
            saved = context.sip_trunks.upsert(trunk)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        result = _reload_asterisk_pjsip(context)
        register_result = _register_trunk(context, saved) if saved.enabled else None
        context.append_security_audit(
            workspace_id=None,
            action="sip_trunk_secret_change",
            actor="api",
            resource_type="sip_trunk",
            resource_id=saved.trunk_id,
            outcome="saved",
            metadata=saved.redacted_dict(),
        )
        return {
            "trunk": saved.redacted_dict(),
            "reload": _control_result_dict(result),
            "register": _control_result_dict(register_result),
        }

    @router.post("/sip-trunks/{trunk_id}/connect")
    def connect_sip_trunk(trunk_id: str) -> dict[str, Any]:
        if context.sip_trunks is None:
            raise HTTPException(status_code=503, detail="SIP trunk registry is not configured")
        try:
            trunk = context.sip_trunks.set_enabled(trunk_id, True)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        if trunk is None:
            raise HTTPException(status_code=404, detail=f"SIP trunk not found: {trunk_id}")
        reload_result = _reload_asterisk_pjsip(context)
        register_result = _register_trunk(context, trunk)
        context.append_security_audit(
            workspace_id=None,
            action="sip_trunk_connect",
            actor="api",
            resource_type="sip_trunk",
            resource_id=trunk.trunk_id,
            outcome="enabled",
            metadata=trunk.redacted_dict(),
        )
        return {
            "trunk": trunk.redacted_dict(),
            "reload": _control_result_dict(reload_result),
            "register": _control_result_dict(register_result),
        }

    @router.post("/sip-trunks/{trunk_id}/disconnect")
    def disconnect_sip_trunk(trunk_id: str) -> dict[str, Any]:
        if context.sip_trunks is None:
            raise HTTPException(status_code=503, detail="SIP trunk registry is not configured")
        try:
            existing = context.sip_trunks.get(trunk_id)
            if existing is None:
                raise HTTPException(status_code=404, detail=f"SIP trunk not found: {trunk_id}")
            unregister_result = _unregister_trunk(context, existing) if existing.enabled else None
            trunk = context.sip_trunks.set_enabled(trunk_id, False)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        reload_result = _reload_asterisk_pjsip(context)
        context.append_security_audit(
            workspace_id=None,
            action="sip_trunk_disconnect",
            actor="api",
            resource_type="sip_trunk",
            resource_id=trunk.trunk_id if trunk is not None else trunk_id,
            outcome="disabled",
            metadata=trunk.redacted_dict() if trunk is not None else {},
        )
        return {
            "trunk": trunk.redacted_dict() if trunk is not None else None,
            "unregister": _control_result_dict(unregister_result),
            "reload": _control_result_dict(reload_result),
        }

    @router.delete("/sip-trunks/{trunk_id}")
    def delete_sip_trunk(trunk_id: str) -> dict[str, Any]:
        if context.sip_trunks is None:
            raise HTTPException(status_code=503, detail="SIP trunk registry is not configured")
        try:
            existing = context.sip_trunks.get(trunk_id)
            if existing is None:
                raise HTTPException(status_code=404, detail=f"SIP trunk not found: {trunk_id}")
            unregister_result = _unregister_trunk(context, existing) if existing.enabled else None
            removed = context.sip_trunks.delete(trunk_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        reload_result = _reload_asterisk_pjsip(context)
        context.append_security_audit(
            workspace_id=None,
            action="sip_trunk_delete",
            actor="api",
            resource_type="sip_trunk",
            resource_id=trunk_id,
            outcome="deleted",
            metadata=removed.redacted_dict() if removed is not None else {},
        )
        return {
            "trunk": removed.redacted_dict() if removed is not None else None,
            "unregister": _control_result_dict(unregister_result),
            "reload": _control_result_dict(reload_result),
        }

    return router


def _reload_asterisk_pjsip(context: SipApiContext):
    return _safe_asterisk_action(context, lambda: context.asterisk.reload_pjsip())


def _register_trunk(context: SipApiContext, trunk: SipTrunk):
    return _safe_asterisk_action(context, lambda: context.asterisk.send_register(trunk.registration_name))


def _unregister_trunk(context: SipApiContext, trunk: SipTrunk):
    return _safe_asterisk_action(context, lambda: context.asterisk.send_unregister(trunk.registration_name))


def _safe_asterisk_action(context: SipApiContext, action):
    if context.asterisk is None:
        return None
    try:
        return action()
    except OSError as exc:
        return ControlResult(False, f"Asterisk AMI request failed: {exc}")


def _control_result_dict(result) -> dict[str, Any] | None:
    if result is None:
        return None
    return {"ok": result.ok, "message": result.message}
