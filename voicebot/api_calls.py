from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, get_args

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from .api_models import (
    AgentResponseRequest,
    CallControlRequest,
    CallMessageRequest,
    MultimodalContentRequest,
    PlaybackInterruptRequest,
)
from .asterisk_control import ControlResult
from .call_recording import recording_artifact_id
from .calls import AgentResponse
from .events import VoicebotEvent, event_to_dict
from .multimodal import ContentDirection, Modality, ModalityCapabilities, MultimodalContent, validate_multimodal_content


@dataclass(frozen=True)
class CallsApiContext:
    registry: Any
    multimodal_store: Any
    events: Any
    broadcast: Callable[[VoicebotEvent], Awaitable[None]]
    multimodal_capabilities: ModalityCapabilities
    audio_artifacts: Any = None
    tracker: Any = None
    asterisk: Any = None
    webrtc: Any = None
    append_security_audit: Callable[..., VoicebotEvent] | None = None


def create_calls_router(context: CallsApiContext) -> APIRouter:
    router = APIRouter()

    @router.get("/calls")
    def list_calls() -> dict[str, Any]:
        return list_calls_payload(context)

    @router.get("/calls/state-store")
    def list_stored_call_states(active_only: bool = False) -> dict[str, Any]:
        return stored_call_states_payload(context, active_only=active_only)

    @router.get("/calls/{call_id}")
    def call_state(call_id: str) -> dict[str, Any]:
        return call_state_payload(context, call_id)

    @router.get("/calls/{call_id}/multimodal")
    def call_multimodal_context(call_id: str) -> dict[str, Any]:
        return context.multimodal_store.get(call_id).to_agent_context()

    @router.get("/calls/{call_id}/recording")
    def get_call_recording_metadata(call_id: str) -> dict[str, Any]:
        record = call_recording_record(context, call_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"Call recording not found: {call_id}")
        return {"artifact_id": record.artifact_id, "metadata": record.metadata}

    @router.get("/calls/{call_id}/recording.wav")
    def get_call_recording_audio(call_id: str) -> Response:
        if context.audio_artifacts is None:
            raise HTTPException(status_code=503, detail="Audio artifact storage is not configured")
        artifact_id = recording_artifact_id(call_id)
        data = context.audio_artifacts.get(artifact_id)
        if data is None:
            raise HTTPException(status_code=404, detail=f"Call recording not found: {call_id}")
        return Response(
            content=data,
            media_type="audio/wav",
            headers={"Content-Disposition": f'inline; filename="{artifact_id}"'},
        )

    @router.post("/calls/{call_id}/messages")
    async def submit_call_message(call_id: str, request: CallMessageRequest) -> dict[str, Any]:
        session = context.registry.get(call_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"Active call not found: {call_id}")
        submit_user_text = getattr(session, "submit_user_text", None)
        if submit_user_text is None:
            raise HTTPException(status_code=400, detail="Call does not support text input")
        try:
            transcript, agent_request = await asyncio.to_thread(submit_user_text, request.text, request.metadata)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        await context.broadcast(transcript)
        await context.broadcast(agent_request)
        return {
            "events": [event_to_dict(transcript), event_to_dict(agent_request)],
            "ok": True,
        }

    @router.post("/calls/{call_id}/multimodal/parts")
    async def add_call_multimodal_part(call_id: str, request: MultimodalContentRequest) -> dict[str, Any]:
        if request.modality not in _MODALITIES:
            raise HTTPException(status_code=400, detail=f"unsupported modality: {request.modality}")
        if request.direction not in _CONTENT_DIRECTIONS:
            raise HTTPException(status_code=400, detail=f"unsupported content direction: {request.direction}")
        part = MultimodalContent(
            modality=request.modality,  # type: ignore[arg-type]
            direction=request.direction,  # type: ignore[arg-type]
            mime_type=request.mime_type,
            uri=request.uri,
            text=request.text,
            metadata=request.metadata,
        )
        validation_issues = validate_multimodal_content(part, context.multimodal_capabilities)
        if validation_issues:
            raise HTTPException(status_code=400, detail=[issue.to_dict() for issue in validation_issues])
        try:
            multimodal_context = context.multimodal_store.add_part(
                call_id,
                part,
                workspace_id=request.workspace_id,
                voicebot_id=request.voicebot_id,
                session_id=request.session_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        event = context.events.append(
            call_id,
            "multimodal_content_added",
            {
                "workspace_id": request.workspace_id,
                "voicebot_id": request.voicebot_id,
                "session_id": request.session_id,
                "part": part.to_agent_part(),
                "part_count": len(multimodal_context.parts),
            },
        )
        await context.broadcast(event)
        return {"context": multimodal_context.to_agent_context(), "event": event_to_dict(event)}

    @router.post("/calls/{call_id}/responses")
    async def submit_response(call_id: str, request: AgentResponseRequest) -> dict[str, Any]:
        return await submit_response_payload(context, call_id, request)

    @router.post("/calls/{call_id}/control")
    async def call_control(call_id: str, request: CallControlRequest) -> dict[str, Any]:
        return await call_control_payload(context, call_id, request)

    @router.post("/calls/{call_id}/playback/interrupt")
    async def interrupt_playback(call_id: str, request: PlaybackInterruptRequest) -> dict[str, Any]:
        return await interrupt_playback_payload(context, call_id, request)

    return router


def list_calls_payload(context: CallsApiContext) -> dict[str, Any]:
    return {"calls": context.registry.snapshots()}


def stored_call_states_payload(context: CallsApiContext, *, active_only: bool = False) -> dict[str, Any]:
    return {"calls": context.registry.stored_snapshots(active_only=active_only)}


def call_state_payload(context: CallsApiContext, call_id: str) -> dict[str, Any]:
    snapshot = context.registry.snapshot(call_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"Active call not found: {call_id}")
    return snapshot


def call_recording_record(context: CallsApiContext, call_id: str):
    if context.audio_artifacts is None:
        raise HTTPException(status_code=503, detail="Audio artifact storage is not configured")
    artifact_id = recording_artifact_id(call_id)
    for record in context.audio_artifacts.list():
        if record.artifact_id == artifact_id:
            return record
    return None


async def submit_response_payload(
    context: CallsApiContext,
    call_id: str,
    request: AgentResponseRequest,
) -> dict[str, Any]:
    if context.tracker is None:
        raise HTTPException(status_code=503, detail="Agent task tracker is not configured")
    session = context.registry.get(call_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Active call not found: {call_id}")
    if request.finalize_only:
        context.tracker.mark_responded(request.response_to_event_id)
        event = context.events.append(
            call_id,
            "agent_response_received",
            {
                "text": "",
                "response_to_event_id": request.response_to_event_id,
                "response_kind": request.response_kind or "stream_finalized",
                "stream_finalized": True,
            },
        )
        await context.broadcast(event)
        return {"event": event_to_dict(event), "ok": True}
    try:
        event = await asyncio.to_thread(
            session.submit_agent_response,
            AgentResponse(
                call_id=call_id,
                text=request.text,
                response_to_event_id=request.response_to_event_id,
                response_kind=request.response_kind,
                partial=request.partial,
                chat=request.chat,
            ),
        )
    except Exception as exc:
        if not request.partial:
            context.tracker.mark_responded(request.response_to_event_id)
        failed = context.events.append(
            call_id,
            "agent_response_dropped",
            {
                "reason": "playback_failed",
                "error": str(exc),
                "response_to_event_id": request.response_to_event_id,
            },
        )
        await context.broadcast(failed)
        return {"event": event_to_dict(failed), "ok": False}
    if not request.partial:
        context.tracker.mark_responded(request.response_to_event_id)
    await context.broadcast(event)
    return {"event": event_to_dict(event), "ok": True}


async def call_control_payload(
    context: CallsApiContext,
    call_id: str,
    request: CallControlRequest,
) -> dict[str, Any]:
    if context.tracker is None:
        raise HTTPException(status_code=503, detail="Agent task tracker is not configured")
    requested = context.events.append(call_id, "call_control_requested", request.model_dump())
    active_session = context.registry.get(call_id)
    active_snapshot = active_session.snapshot() if active_session is not None else None
    route = active_snapshot.get("route") if isinstance(active_snapshot, dict) else {}
    if not isinstance(route, dict):
        route = {}
    audit_session_id = None
    if isinstance(active_snapshot, dict):
        audit_session_id = route.get("session_id") or active_snapshot.get("session_id")
    if context.append_security_audit is not None:
        audit_event = context.append_security_audit(
            workspace_id=route.get("workspace_id"),
            voicebot_id=route.get("voicebot_id"),
            session_id=audit_session_id,
            call_id=call_id,
            action=f"call_control.{request.action}",
            actor="agent_or_api",
            resource_type="call",
            resource_id=call_id,
            outcome="requested",
            metadata=request.model_dump(),
        )
        await context.broadcast(audit_event)
    transport = active_snapshot.get("transport") if isinstance(active_snapshot, dict) else None

    if transport == "webrtc":
        if request.action == "hangup":
            closed = False
            if context.webrtc is not None:
                closed = await context.webrtc.close_call(call_id)
            elif active_session is not None:
                active_session.stop()
                context.registry.remove(call_id)
                closed = True
            completed = context.events.append(
                call_id,
                "call_control_completed",
                {
                    "action": request.action,
                    "ok": closed,
                    "message": "WebRTC call closed" if closed else "WebRTC call not found",
                    "request_event_id": requested.id,
                },
            )
            context.tracker.mark_responded(request.response_to_event_id)
            await context.broadcast(completed)
            return {"event": event_to_dict(completed)}

        completed = context.events.append(
            call_id,
            "call_control_completed",
            {
                "action": request.action,
                "ok": False,
                "message": f"{request.action} is not supported for WebRTC calls yet",
                "request_event_id": requested.id,
            },
        )
        context.tracker.mark_responded(request.response_to_event_id)
        await context.broadcast(completed)
        return {"event": event_to_dict(completed)}

    if context.asterisk is None:
        completed = context.events.append(
            call_id,
            "call_control_completed",
            {
                "action": request.action,
                "ok": False,
                "message": "Asterisk AMI control is not configured",
                "request_event_id": requested.id,
            },
        )
        context.tracker.mark_responded(request.response_to_event_id)
        await context.broadcast(completed)
        raise HTTPException(status_code=503, detail="Asterisk AMI control is not configured")

    try:
        if request.action == "hangup":
            result = context.asterisk.hangup(call_id)
        elif request.action == "transfer" and request.target:
            result = context.asterisk.transfer(call_id, validated_transfer_target(request.target))
        elif request.action == "transfer":
            completed = context.events.append(
                call_id,
                "call_control_completed",
                {
                    "action": request.action,
                    "ok": False,
                    "message": "transfer requires target",
                    "request_event_id": requested.id,
                },
            )
            context.tracker.mark_responded(request.response_to_event_id)
            await context.broadcast(completed)
            raise HTTPException(status_code=400, detail="transfer requires target")
        elif request.action == "send_dtmf" and request.digit:
            result = context.asterisk.send_dtmf(call_id, validated_dtmf_digit(request.digit))
        elif request.action == "send_dtmf":
            completed = context.events.append(
                call_id,
                "call_control_completed",
                {
                    "action": request.action,
                    "ok": False,
                    "message": "send_dtmf requires digit",
                    "request_event_id": requested.id,
                },
            )
            context.tracker.mark_responded(request.response_to_event_id)
            await context.broadcast(completed)
            raise HTTPException(status_code=400, detail="send_dtmf requires digit")
        else:
            completed = context.events.append(
                call_id,
                "call_control_completed",
                {
                    "action": request.action,
                    "ok": False,
                    "message": f"unsupported control action: {request.action}",
                    "request_event_id": requested.id,
                },
            )
            context.tracker.mark_responded(request.response_to_event_id)
            await context.broadcast(completed)
            raise HTTPException(status_code=400, detail=f"unsupported control action: {request.action}")
    except HTTPException:
        raise
    except Exception as exc:
        result = ControlResult(False, f"Asterisk AMI request failed: {exc}")

    completed = context.events.append(
        call_id,
        "call_control_completed",
        {"action": request.action, "ok": result.ok, "message": result.message, "request_event_id": requested.id},
    )
    context.tracker.mark_responded(request.response_to_event_id)
    await context.broadcast(completed)
    return {"event": event_to_dict(completed)}


async def interrupt_playback_payload(
    context: CallsApiContext,
    call_id: str,
    request: PlaybackInterruptRequest,
) -> dict[str, Any]:
    if context.tracker is None:
        raise HTTPException(status_code=503, detail="Agent task tracker is not configured")
    session = context.registry.get(call_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Active call not found: {call_id}")
    event = session.interrupt_playback(request.reason)
    context.tracker.mark_responded(request.response_to_event_id)
    await context.broadcast(event)
    return {"event": event_to_dict(event)}


def validated_dtmf_digit(value: Any) -> str:
    digit = str(value).upper()
    if len(digit) != 1 or digit not in "0123456789*#ABCD":
        raise HTTPException(status_code=400, detail="digit must be one DTMF character: 0-9, *, #, A-D")
    return digit


def validated_transfer_target(value: Any) -> str:
    target = str(value).strip()
    if not target:
        raise HTTPException(status_code=400, detail="transfer target must not be empty")
    if len(target) > 128:
        raise HTTPException(status_code=400, detail="transfer target must be at most 128 characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in target):
        raise HTTPException(status_code=400, detail="transfer target must not contain control characters")
    return target


_MODALITIES = set(get_args(Modality))
_CONTENT_DIRECTIONS = set(get_args(ContentDirection))
