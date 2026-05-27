from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect

from .agent_tasks import AgentTaskTracker
from .api_models import (
    AgentResponseRequest,
    AgentTaskClaimRequest,
    AgentTaskReleaseRequest,
    AgentToolRequest,
    CallControlRequest,
    CompactContextRequest,
    PlaybackInterruptRequest,
)
from .asterisk_control import AsteriskAMI
from .calls import AgentResponse, CallRegistry
from .event_catalog import event_catalog
from .events import EventStore, VoicebotEvent, event_to_dict
from .metrics import summarize_metrics
from .provider_catalog import provider_catalog
from .tool_executor import AgentToolExecutor
from .transcripts import TranscriptStore
from .tools import tool_definitions_json_schema, tool_definitions_legacy


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
    tool_executor = AgentToolExecutor()

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "active_calls": registry.active_call_ids()}

    @app.get("/calls")
    def list_calls() -> dict[str, Any]:
        return {"calls": registry.snapshots()}

    @app.get("/calls/{call_id}")
    def call_state(call_id: str) -> dict[str, Any]:
        snapshot = registry.snapshot(call_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail=f"Active call not found: {call_id}")
        return snapshot

    @app.get("/providers")
    def providers() -> dict[str, Any]:
        return provider_catalog()

    @app.get("/events")
    def list_events(after: int = 0, call_id: str | None = None, limit: int = 200) -> dict[str, Any]:
        result = [event_to_dict(event) for event in events.list_events(after=after, call_id=call_id, limit=limit)]
        return {"events": result}

    @app.get("/events/catalog")
    def list_event_catalog() -> dict[str, Any]:
        return {"events": event_catalog()}

    @app.get("/metrics")
    def metrics(call_id: str | None = None) -> dict[str, Any]:
        return summarize_metrics(events.list_events(call_id=call_id, limit=1000))

    @app.get("/context")
    def context(call_id: str | None = None) -> dict[str, Any]:
        return events.context(call_id=call_id)

    @app.post("/context/compact")
    async def compact_context(request: CompactContextRequest) -> dict[str, Any]:
        event = events.replace_summary(request.summary, call_id=request.call_id)
        await hub.broadcast(event)
        return {"event": event_to_dict(event)}

    @app.get("/agent/tasks")
    def agent_tasks(after: int = 0, call_id: str | None = None, limit: int = 200) -> dict[str, Any]:
        all_events = [
            event
            for event in events.list_events(after=after, limit=1000)
            if event.type == "agent_response_requested"
        ]
        active_call_ids = set(registry.active_call_ids())
        pending = [
            event
            for event in all_events
            if event.type == "agent_response_requested"
            and tracker.is_pending(event.id)
            and event.call_id in active_call_ids
            and (call_id is None or event.call_id == call_id)
        ]
        return {
            "pending": [event_to_dict(event) for event in pending[:limit]],
            "context": events.context(call_id=call_id),
        }

    @app.post("/agent/tasks/claim")
    def claim_agent_tasks(request: AgentTaskClaimRequest) -> dict[str, Any]:
        active_call_ids = set(registry.active_call_ids())
        eligible_event_ids = []
        for event_id in request.event_ids:
            source_event = events.get_event(event_id)
            if (
                source_event is not None
                and source_event.type == "agent_response_requested"
                and source_event.call_id in active_call_ids
            ):
                eligible_event_ids.append(event_id)

        claimed_event_ids = tracker.claim(eligible_event_ids, request.owner, request.ttl_seconds)
        for event_id in claimed_event_ids:
            source_event = events.get_event(event_id)
            if source_event is None:
                continue
            events.append(
                source_event.call_id,
                "agent_task_claimed",
                {
                    "task_event_id": event_id,
                    "owner": request.owner,
                    "ttl_seconds": request.ttl_seconds,
                },
            )
        return {
            "claimed_event_ids": claimed_event_ids,
            "owner": request.owner,
        }

    @app.post("/agent/tasks/release")
    def release_agent_tasks(request: AgentTaskReleaseRequest) -> dict[str, Any]:
        released_event_ids = tracker.release_many(request.event_ids, owner=request.owner)
        for event_id in released_event_ids:
            source_event = events.get_event(event_id)
            if source_event is None:
                continue
            events.append(
                source_event.call_id,
                "agent_task_released",
                {"task_event_id": event_id, "owner": request.owner},
            )
        return {"released_event_ids": released_event_ids}

    @app.get("/agent/tasks/status")
    def agent_task_status() -> dict[str, Any]:
        return tracker.snapshot()

    @app.get("/agent/tools")
    def agent_tools() -> dict[str, Any]:
        return {"tools": tool_definitions_legacy()}

    @app.get("/agent/tools/schema")
    def agent_tool_schema() -> dict[str, Any]:
        return {"tools": tool_definitions_json_schema()}

    @app.post("/agent/tools/{tool_name}")
    async def agent_tool(tool_name: str, request: AgentToolRequest) -> dict[str, Any]:
        try:
            return await tool_executor.execute(tool_name, request.arguments)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown agent tool: {tool_name}") from None

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

    @app.get("/transcripts")
    def list_transcripts() -> dict[str, Any]:
        return {"call_ids": transcripts.list_call_ids()}

    @app.post("/calls/{call_id}/control")
    async def call_control(call_id: str, request: CallControlRequest) -> dict[str, Any]:
        requested = events.append(call_id, "call_control_requested", request.model_dump())
        if asterisk is None:
            completed = events.append(
                call_id,
                "call_control_completed",
                {
                    "action": request.action,
                    "ok": False,
                    "message": "Asterisk AMI control is not configured",
                    "request_event_id": requested.id,
                },
            )
            tracker.mark_responded(request.response_to_event_id)
            await hub.broadcast(completed)
            raise HTTPException(status_code=503, detail="Asterisk AMI control is not configured")

        if request.action == "hangup":
            result = asterisk.hangup(call_id)
        elif request.action == "transfer" and request.target:
            result = asterisk.transfer(call_id, request.target)
        elif request.action == "transfer":
            completed = events.append(
                call_id,
                "call_control_completed",
                {
                    "action": request.action,
                    "ok": False,
                    "message": "transfer requires target",
                    "request_event_id": requested.id,
                },
            )
            tracker.mark_responded(request.response_to_event_id)
            await hub.broadcast(completed)
            raise HTTPException(status_code=400, detail="transfer requires target")
        elif request.action == "send_dtmf" and request.digit:
            result = asterisk.send_dtmf(call_id, request.digit)
        elif request.action == "send_dtmf":
            completed = events.append(
                call_id,
                "call_control_completed",
                {
                    "action": request.action,
                    "ok": False,
                    "message": "send_dtmf requires digit",
                    "request_event_id": requested.id,
                },
            )
            tracker.mark_responded(request.response_to_event_id)
            await hub.broadcast(completed)
            raise HTTPException(status_code=400, detail="send_dtmf requires digit")
        else:
            completed = events.append(
                call_id,
                "call_control_completed",
                {
                    "action": request.action,
                    "ok": False,
                    "message": f"unsupported control action: {request.action}",
                    "request_event_id": requested.id,
                },
            )
            tracker.mark_responded(request.response_to_event_id)
            await hub.broadcast(completed)
            raise HTTPException(status_code=400, detail=f"unsupported control action: {request.action}")

        completed = events.append(
            call_id,
            "call_control_completed",
            {"action": request.action, "ok": result.ok, "message": result.message, "request_event_id": requested.id},
        )
        tracker.mark_responded(request.response_to_event_id)
        await hub.broadcast(completed)
        return {"event": event_to_dict(completed)}

    @app.post("/calls/{call_id}/playback/interrupt")
    async def interrupt_playback(call_id: str, request: PlaybackInterruptRequest) -> dict[str, Any]:
        session = registry.get(call_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"Active call not found: {call_id}")
        event = session.interrupt_playback(request.reason)
        tracker.mark_responded(request.response_to_event_id)
        await hub.broadcast(event)
        return {"event": event_to_dict(event)}

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

    async def tool_say(args: dict[str, Any]) -> dict[str, Any]:
        call_id = require_arg(args, "call_id")
        text = require_arg(args, "text")
        response = AgentResponseRequest(
            text=text,
            response_to_event_id=args.get("response_to_event_id"),
        )
        return await submit_response(call_id, response)

    async def tool_hangup_call(args: dict[str, Any]) -> dict[str, Any]:
        call_id = require_arg(args, "call_id")
        return await call_control(
            call_id,
            CallControlRequest(action="hangup", response_to_event_id=args.get("response_to_event_id")),
        )

    async def tool_transfer_call(args: dict[str, Any]) -> dict[str, Any]:
        call_id = require_arg(args, "call_id")
        target = require_arg(args, "target")
        return await call_control(
            call_id,
            CallControlRequest(
                action="transfer",
                target=target,
                response_to_event_id=args.get("response_to_event_id"),
            ),
        )

    async def tool_send_dtmf(args: dict[str, Any]) -> dict[str, Any]:
        call_id = require_arg(args, "call_id")
        digit = require_arg(args, "digit")
        return await call_control(
            call_id,
            CallControlRequest(
                action="send_dtmf",
                digit=digit,
                response_to_event_id=args.get("response_to_event_id"),
            ),
        )

    async def tool_stop_playback(args: dict[str, Any]) -> dict[str, Any]:
        call_id = require_arg(args, "call_id")
        return await interrupt_playback(
            call_id,
            PlaybackInterruptRequest(
                reason=str(args.get("reason") or "agent_requested"),
                response_to_event_id=args.get("response_to_event_id"),
            ),
        )

    def tool_get_transcript(args: dict[str, Any]) -> dict[str, Any]:
        call_id = require_arg(args, "call_id")
        return call_transcript(call_id)

    def tool_list_transcripts(args: dict[str, Any]) -> dict[str, Any]:
        return list_transcripts()

    def tool_get_events(args: dict[str, Any]) -> dict[str, Any]:
        return list_events(
            after=int(args.get("after", 0)),
            call_id=args.get("call_id"),
            limit=int(args.get("limit", 200)),
        )

    def tool_get_metrics(args: dict[str, Any]) -> dict[str, Any]:
        return metrics(call_id=args.get("call_id"))

    def tool_get_active_calls(args: dict[str, Any]) -> dict[str, Any]:
        return {"active_calls": registry.active_call_ids()}

    def tool_get_call_state(args: dict[str, Any]) -> dict[str, Any]:
        call_id = require_arg(args, "call_id")
        return call_state(call_id)

    tool_executor.register("say", tool_say)
    tool_executor.register("hangup_call", tool_hangup_call)
    tool_executor.register("transfer_call", tool_transfer_call)
    tool_executor.register("send_dtmf", tool_send_dtmf)
    tool_executor.register("stop_playback", tool_stop_playback)
    tool_executor.register("list_transcripts", tool_list_transcripts)
    tool_executor.register("get_transcript", tool_get_transcript)
    tool_executor.register("get_events", tool_get_events)
    tool_executor.register("get_metrics", tool_get_metrics)
    tool_executor.register("get_active_calls", tool_get_active_calls)
    tool_executor.register("get_call_state", tool_get_call_state)

    return app


def require_arg(args: dict[str, Any], name: str) -> Any:
    value = args.get(name)
    if value is None or value == "":
        raise HTTPException(status_code=400, detail=f"missing required tool argument: {name}")
    return value
