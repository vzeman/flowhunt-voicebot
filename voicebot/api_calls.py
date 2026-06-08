from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, get_args

from fastapi import APIRouter, HTTPException

from .api_models import MultimodalContentRequest
from .events import VoicebotEvent, event_to_dict
from .multimodal import ContentDirection, Modality, ModalityCapabilities, MultimodalContent, validate_multimodal_content


@dataclass(frozen=True)
class CallsApiContext:
    registry: Any
    multimodal_store: Any
    events: Any
    broadcast: Callable[[VoicebotEvent], Awaitable[None]]
    multimodal_capabilities: ModalityCapabilities


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


_MODALITIES = set(get_args(Modality))
_CONTENT_DIRECTIONS = set(get_args(ContentDirection))
