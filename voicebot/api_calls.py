from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, get_args

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from .api_models import CallMessageRequest, MultimodalContentRequest
from .call_recording import recording_artifact_id
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


_MODALITIES = set(get_args(Modality))
_CONTENT_DIRECTIONS = set(get_args(ContentDirection))
