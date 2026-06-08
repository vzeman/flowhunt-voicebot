from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, HTTPException

from .events import VoicebotEvent, event_to_dict
from .subagents import subagent_task_to_dict
from .transports import transport_catalog


@dataclass(frozen=True)
class VoicebotSessionsApiContext:
    voicebot_session_store: Any
    events: Any
    transcripts: Any
    subagent_coordinator: Any
    require_workspace_access: Callable[[str], None]
    durable_call_events: Callable[..., list[VoicebotEvent]]
    validated_limit: Callable[[int], int]
    append_security_audit: Callable[..., VoicebotEvent]
    broadcast: Callable[[VoicebotEvent], Awaitable[None]]


def create_voicebot_sessions_router(context: VoicebotSessionsApiContext) -> APIRouter:
    router = APIRouter()

    @router.get("/workspaces/{workspace_id}/voicebots/{voicebot_id}/sessions")
    def list_voicebot_sessions(
        workspace_id: str,
        voicebot_id: str,
        active_only: bool = False,
    ) -> dict[str, Any]:
        context.require_workspace_access(workspace_id)
        sessions = context.voicebot_session_store.list(
            workspace_id=workspace_id,
            voicebot_id=voicebot_id,
            active_only=active_only,
        )
        return {
            "workspace_id": workspace_id,
            "voicebot_id": voicebot_id,
            "sessions": [session.as_dict() for session in sessions],
        }

    @router.get("/workspaces/{workspace_id}/voicebots/{voicebot_id}/sessions/{session_id}")
    def get_voicebot_session(workspace_id: str, voicebot_id: str, session_id: str) -> dict[str, Any]:
        context.require_workspace_access(workspace_id)
        session = _session_or_404(context, workspace_id, voicebot_id, session_id)
        return {"session": session.as_dict()}

    @router.get("/workspaces/{workspace_id}/voicebots/{voicebot_id}/sessions/{session_id}/timeline")
    def get_voicebot_session_timeline(
        workspace_id: str,
        voicebot_id: str,
        session_id: str,
        after: int = 0,
        limit: int = 200,
    ) -> dict[str, Any]:
        context.require_workspace_access(workspace_id)
        session = _session_or_404(context, workspace_id, voicebot_id, session_id)
        call_id = session.external_session_id or session.session_id
        timeline = context.durable_call_events(
            context.events,
            context.transcripts,
            call_id,
            after=after,
            limit=context.validated_limit(limit),
        )
        return {
            "workspace_id": workspace_id,
            "voicebot_id": voicebot_id,
            "session_id": session_id,
            "call_id": call_id,
            "events": [event_to_dict(event) for event in timeline],
        }

    @router.get("/workspaces/{workspace_id}/voicebots/{voicebot_id}/sessions/{session_id}/transcript")
    async def get_voicebot_session_transcript(
        workspace_id: str,
        voicebot_id: str,
        session_id: str,
        after: int = 0,
        limit: int = 200,
    ) -> dict[str, Any]:
        context.require_workspace_access(workspace_id)
        session = _session_or_404(context, workspace_id, voicebot_id, session_id)
        call_id = session.external_session_id or session.session_id
        transcript = context.transcripts.read(call_id, after=after, limit=context.validated_limit(limit))
        audit_event = context.append_security_audit(
            workspace_id=workspace_id,
            voicebot_id=voicebot_id,
            session_id=session_id,
            call_id=call_id,
            action="transcript_read",
            actor="api",
            resource_type="transcript",
            resource_id=session_id,
            outcome="read",
            metadata={"after": after, "limit": limit, "event_count": len(transcript)},
        )
        await context.broadcast(audit_event)
        return {
            "workspace_id": workspace_id,
            "voicebot_id": voicebot_id,
            "session_id": session_id,
            "call_id": call_id,
            "events": transcript,
        }

    @router.get("/workspaces/{workspace_id}/voicebots/{voicebot_id}/tasks")
    def list_voicebot_external_tasks(
        workspace_id: str,
        voicebot_id: str,
        session_id: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        context.require_workspace_access(workspace_id)
        if context.subagent_coordinator is None:
            raise HTTPException(status_code=503, detail="Subagent coordinator is not configured")
        tasks = [
            task
            for task in context.subagent_coordinator.store.list(workspace_id=workspace_id, session_id=session_id)
            if task.voicebot_id == voicebot_id and (status is None or task.status == status)
        ]
        return {
            "workspace_id": workspace_id,
            "voicebot_id": voicebot_id,
            "session_id": session_id,
            "status": status,
            "tasks": [subagent_task_to_dict(task) for task in tasks],
        }

    @router.get("/workspaces/{workspace_id}/voicebots/{voicebot_id}/transports")
    def get_voicebot_transport_catalog(
        workspace_id: str,
        voicebot_id: str,
        include_health: bool = False,
    ) -> dict[str, Any]:
        context.require_workspace_access(workspace_id)
        return {
            "workspace_id": workspace_id,
            "voicebot_id": voicebot_id,
            **transport_catalog(include_health=include_health),
        }

    return router


def _session_or_404(context: VoicebotSessionsApiContext, workspace_id: str, voicebot_id: str, session_id: str):
    session = context.voicebot_session_store.get(session_id, workspace_id=workspace_id)
    if session is None or session.voicebot_id != voicebot_id:
        raise HTTPException(status_code=404, detail="Session not found")
    return session
