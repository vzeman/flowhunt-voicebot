from __future__ import annotations

from dataclasses import dataclass
import asyncio
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from .asterisk_control import AsteriskAMI
from .calls import AgentResponse, CallRegistry
from .events import EventStore, VoicebotEvent, event_to_dict
from .transcripts import TranscriptStore


class AgentResponseRequest(BaseModel):
    text: str
    response_to_event_id: int | None = None


class CompactContextRequest(BaseModel):
    summary: str
    call_id: str = "system"


class CallControlRequest(BaseModel):
    action: str
    target: str | None = None


class AgentToolRequest(BaseModel):
    arguments: dict[str, Any] = {}


@dataclass
class AgentTaskTracker:
    responded_event_ids: set[int]

    def __init__(self) -> None:
        self.responded_event_ids = set()

    def mark_responded(self, event_id: int | None) -> None:
        if event_id is not None:
            self.responded_event_ids.add(event_id)


class WebSocketHub:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)

    async def broadcast(self, event: VoicebotEvent) -> None:
        payload = event_to_dict(event)
        dead: list[WebSocket] = []
        for websocket in self._connections:
            try:
                await websocket.send_json(payload)
            except RuntimeError:
                dead.append(websocket)
        for websocket in dead:
            self.disconnect(websocket)


class BroadcastingEventStore(EventStore):
    def __init__(self, max_context_events: int, hub: WebSocketHub) -> None:
        super().__init__(max_context_events)
        self.hub = hub

    def append(self, call_id: str, event_type, data: dict[str, Any] | None = None) -> VoicebotEvent:
        event = super().append(call_id, event_type, data)
        # Broadcast from request handlers directly where an event loop exists.
        return event


def create_app(
    events: EventStore,
    registry: CallRegistry,
    tracker: AgentTaskTracker,
    hub: WebSocketHub,
    transcripts: TranscriptStore,
    asterisk: AsteriskAMI | None,
) -> FastAPI:
    app = FastAPI(title="Flowhunt Voicebot", version="0.1.0")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "active_calls": registry.active_call_ids()}

    @app.get("/events")
    def list_events(after: int = 0, call_id: str | None = None, limit: int = 200) -> dict[str, Any]:
        result = [event_to_dict(event) for event in events.list_events(after=after, call_id=call_id, limit=limit)]
        return {"events": result}

    @app.get("/context")
    def context(call_id: str | None = None) -> dict[str, Any]:
        return events.context(call_id=call_id)

    @app.post("/context/compact")
    async def compact_context(request: CompactContextRequest) -> dict[str, Any]:
        event = events.replace_summary(request.summary, call_id=request.call_id)
        await hub.broadcast(event)
        return {"event": event_to_dict(event)}

    @app.get("/agent/tasks")
    def agent_tasks(after: int = 0) -> dict[str, Any]:
        all_events = events.list_events(after=after, limit=1000)
        pending = [
            event
            for event in all_events
            if event.type == "agent_response_requested" and event.id not in tracker.responded_event_ids
        ]
        return {
            "pending": [event_to_dict(event) for event in pending],
            "context": events.context(),
        }

    @app.get("/agent/tools")
    def agent_tools() -> dict[str, Any]:
        return {
            "tools": [
                {
                    "name": "say",
                    "description": "Speak text into an active call.",
                    "arguments": {
                        "call_id": "Active call ID.",
                        "text": "Text to synthesize and play.",
                        "response_to_event_id": "Optional event ID this answers.",
                    },
                },
                {
                    "name": "hangup_call",
                    "description": "Hang up an active call through Asterisk AMI.",
                    "arguments": {"call_id": "Active call ID."},
                },
                {
                    "name": "transfer_call",
                    "description": "Transfer an active call to another SIP extension or target.",
                    "arguments": {"call_id": "Active call ID.", "target": "Extension or SIP target."},
                },
                {
                    "name": "get_transcript",
                    "description": "Read the full persisted transcript/events for one call.",
                    "arguments": {"call_id": "Call ID."},
                },
                {
                    "name": "get_events",
                    "description": "Read recent in-memory events.",
                    "arguments": {
                        "after": "Optional event ID cursor.",
                        "call_id": "Optional call filter.",
                        "limit": "Optional maximum number of events.",
                    },
                },
                {
                    "name": "get_active_calls",
                    "description": "List currently active call IDs.",
                    "arguments": {},
                },
            ]
        }

    @app.post("/agent/tools/{tool_name}")
    async def agent_tool(tool_name: str, request: AgentToolRequest) -> dict[str, Any]:
        args = request.arguments
        if tool_name == "say":
            call_id = require_arg(args, "call_id")
            text = require_arg(args, "text")
            response = AgentResponseRequest(
                text=text,
                response_to_event_id=args.get("response_to_event_id"),
            )
            return await submit_response(call_id, response)
        if tool_name == "hangup_call":
            call_id = require_arg(args, "call_id")
            return await call_control(call_id, CallControlRequest(action="hangup"))
        if tool_name == "transfer_call":
            call_id = require_arg(args, "call_id")
            target = require_arg(args, "target")
            return await call_control(call_id, CallControlRequest(action="transfer", target=target))
        if tool_name == "get_transcript":
            call_id = require_arg(args, "call_id")
            return call_transcript(call_id)
        if tool_name == "get_events":
            return list_events(
                after=int(args.get("after", 0)),
                call_id=args.get("call_id"),
                limit=int(args.get("limit", 200)),
            )
        if tool_name == "get_active_calls":
            return {"active_calls": registry.active_call_ids()}
        raise HTTPException(status_code=404, detail=f"unknown agent tool: {tool_name}")

    @app.post("/calls/{call_id}/responses")
    async def submit_response(call_id: str, request: AgentResponseRequest) -> dict[str, Any]:
        session = registry.get(call_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"Active call not found: {call_id}")
        event = session.submit_agent_response(
            AgentResponse(
                call_id=call_id,
                text=request.text,
                response_to_event_id=request.response_to_event_id,
            )
        )
        tracker.mark_responded(request.response_to_event_id)
        await hub.broadcast(event)
        return {"event": event_to_dict(event)}

    @app.get("/calls/{call_id}/transcript")
    def call_transcript(call_id: str) -> dict[str, Any]:
        return {"call_id": call_id, "events": transcripts.read(call_id)}

    @app.post("/calls/{call_id}/control")
    async def call_control(call_id: str, request: CallControlRequest) -> dict[str, Any]:
        if asterisk is None:
            raise HTTPException(status_code=503, detail="Asterisk AMI control is not configured")

        requested = events.append(call_id, "call_control_requested", request.model_dump())
        if request.action == "hangup":
            result = asterisk.hangup(call_id)
        elif request.action == "transfer":
            if not request.target:
                raise HTTPException(status_code=400, detail="transfer requires target")
            result = asterisk.transfer(call_id, request.target)
        else:
            raise HTTPException(status_code=400, detail=f"unsupported control action: {request.action}")

        completed = events.append(
            call_id,
            "call_control_completed",
            {"action": request.action, "ok": result.ok, "message": result.message, "request_event_id": requested.id},
        )
        await hub.broadcast(completed)
        return {"event": event_to_dict(completed)}

    @app.websocket("/ws/events")
    async def websocket_events(websocket: WebSocket) -> None:
        await hub.connect(websocket)
        last_id = 0
        try:
            while True:
                new_events = events.list_events(after=last_id, limit=100)
                for event in new_events:
                    await websocket.send_json(event_to_dict(event))
                    last_id = max(last_id, event.id)
                await asyncio.sleep(0.25)
        except WebSocketDisconnect:
            hub.disconnect(websocket)

    return app


def require_arg(args: dict[str, Any], name: str) -> Any:
    value = args.get(name)
    if value is None or value == "":
        raise HTTPException(status_code=400, detail=f"missing required tool argument: {name}")
    return value
