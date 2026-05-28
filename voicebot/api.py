from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from .agent_tasks import AgentTaskTracker
from .api_models import (
    AgentResponseRequest,
    AgentTaskClaimRequest,
    AgentTaskReleaseRequest,
    AgentTaskRenewRequest,
    AgentToolRequest,
    CallControlRequest,
    CompactContextRequest,
    ConversationEvaluationRequest,
    PlaybackInterruptRequest,
    ScalingWorkloadPlanRequest,
    SipTrunkRequest,
    WebRTCOfferRequest,
)
from .asterisk_control import AsteriskAMI, ControlResult
from .calls import AgentResponse, CallRegistry
from .config import Settings, redacted_settings
from .event_catalog import event_catalog
from .events import EventStore, VoicebotEvent, event_to_dict
from .execution_model import ExecutionScope
from .flowhunt import (
    FlowHuntClient,
    FlowHuntResult,
    extract_flow_task_error,
    extract_flow_task_result,
    extract_issue_id,
    extract_issue_result,
    extract_issue_state,
    extract_issue_updates,
    extract_session_id,
    is_flow_task_terminal,
    is_terminal_issue_state,
)
from .health import readiness_report
from .metrics import summarize_metrics
from .observability import ConversationExpectation, build_timeline, evaluate_conversation
from .provider_catalog import provider_catalog
from .scaling import WorkloadProfile, build_workload_plan, default_deployment_topology
from .sip_trunks import SipTrunk, SipTrunkStore
from .subagents import SubagentCoordinator, SubagentTask, SubagentTaskRequest, subagent_task_to_dict
from .task_lifecycle import PollingPolicy, SubagentTaskLifecycleRunner, TaskLifecycleEventType
from .tool_executor import AgentToolExecutor
from .transcripts import TranscriptStore
from .tools import tool_definitions_json_schema, tool_definitions_legacy
from .webrtc import WebRTCSessionManager


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
    settings: Settings | None = None,
    sip_trunks: SipTrunkStore | None = None,
    webrtc: WebRTCSessionManager | None = None,
    subagent_coordinator: SubagentCoordinator | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if subagent_lifecycle is None:
            yield
            return
        stop = asyncio.Event()
        app.state.subagent_lifecycle_stop = stop
        app.state.subagent_lifecycle_task = asyncio.create_task(run_subagent_lifecycle_loop(stop))
        try:
            yield
        finally:
            stop.set()
            task = getattr(app.state, "subagent_lifecycle_task", None)
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    app = FastAPI(title="Flowhunt Voicebot", version="0.1.0", lifespan=lifespan)
    tool_executor = AgentToolExecutor()
    runtime_settings = settings or Settings()
    subagent_terminal_events: list[VoicebotEvent] = []

    def emit_subagent_terminal_event(event_type: TaskLifecycleEventType, task: SubagentTask) -> None:
        event = events.append_scoped(
            ExecutionScope(
                workspace_id=task.workspace_id,
                voicebot_id=task.voicebot_id or "",
                session_id=task.session_id,
                call_id=task.session_id,
            ),
            event_type,
            task.event_context(),
        )
        subagent_terminal_events.append(event)

    subagent_lifecycle = (
        SubagentTaskLifecycleRunner(
            subagent_coordinator,
            policy=PollingPolicy(
                initial_interval_seconds=runtime_settings.subagent_task_initial_poll_seconds,
                max_interval_seconds=runtime_settings.subagent_task_max_poll_seconds,
                timeout_seconds=runtime_settings.subagent_task_timeout_seconds,
                max_attempts=runtime_settings.subagent_task_max_attempts,
            ),
            event_sink=emit_subagent_terminal_event,
            session_active=lambda session_id: registry.get(session_id) is not None,
        )
        if subagent_coordinator is not None
        else None
    )

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "active_calls": registry.active_call_ids()}

    @app.get("/health/readiness")
    def readiness() -> dict[str, Any]:
        return readiness_report(
            transcripts=transcripts,
            asterisk=asterisk,
            active_call_ids=registry.active_call_ids(),
        )

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

    @app.get("/scaling/topology")
    def scaling_topology() -> dict[str, Any]:
        return default_deployment_topology().as_dict()

    @app.post("/scaling/workload-plan")
    def scaling_workload_plan(request: ScalingWorkloadPlanRequest) -> dict[str, Any]:
        try:
            profile = WorkloadProfile(
                workspace_id=request.workspace_id,
                voicebot_id=request.voicebot_id,
                concurrent_sessions=request.concurrent_sessions,
                session_id=request.session_id,
                stt_provider=request.stt_provider,
                tts_provider=request.tts_provider,
                agent_provider=request.agent_provider,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return build_workload_plan(profile)

    @app.get("/config")
    def config() -> dict[str, Any]:
        return {"settings": redacted_settings(runtime_settings)}

    @app.get("/webrtc/sessions")
    def list_webrtc_sessions() -> dict[str, Any]:
        if webrtc is None:
            raise HTTPException(status_code=503, detail="WebRTC transport is not configured")
        return {"sessions": webrtc.snapshots()}

    @app.post("/webrtc/sessions")
    async def create_webrtc_session(request: WebRTCOfferRequest) -> dict[str, Any]:
        if webrtc is None:
            raise HTTPException(status_code=503, detail="WebRTC transport is not configured")
        if request.type != "offer":
            raise HTTPException(status_code=400, detail="WebRTC session type must be offer")
        try:
            return await webrtc.create_session(request.sdp, request.type, request.metadata)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None

    @app.delete("/webrtc/sessions/{session_id}")
    async def delete_webrtc_session(session_id: str) -> dict[str, Any]:
        if webrtc is None:
            raise HTTPException(status_code=503, detail="WebRTC transport is not configured")
        closed = await webrtc.close_session(session_id)
        if not closed:
            raise HTTPException(status_code=404, detail=f"WebRTC session not found: {session_id}")
        return {"closed": True, "session_id": session_id}

    @app.get("/webrtc/test")
    def webrtc_test_page() -> HTMLResponse:
        return HTMLResponse(WEBRTC_TEST_PAGE)

    @app.get("/sip-trunks")
    def list_sip_trunks() -> dict[str, Any]:
        if sip_trunks is None:
            raise HTTPException(status_code=503, detail="SIP trunk registry is not configured")
        return {
            "trunks": [trunk.redacted_dict() for trunk in sip_trunks.list()],
            "registrations": control_result_dict(safe_asterisk_action(lambda: asterisk.show_registrations())),
        }

    @app.post("/sip-trunks")
    def upsert_sip_trunk(request: SipTrunkRequest) -> dict[str, Any]:
        if sip_trunks is None:
            raise HTTPException(status_code=503, detail="SIP trunk registry is not configured")
        try:
            trunk = SipTrunk(
                trunk_id=request.trunk_id,
                host=request.host,
                user=request.user,
                password=request.password,
                display_name=request.display_name,
                enabled=request.enabled,
                codecs=tuple(request.codecs),
                expiration=request.expiration,
                retry_interval=request.retry_interval,
                forbidden_retry_interval=request.forbidden_retry_interval,
            )
            saved = sip_trunks.upsert(trunk)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        result = reload_asterisk_pjsip()
        register_result = register_trunk(saved) if saved.enabled else None
        return {
            "trunk": saved.redacted_dict(),
            "reload": control_result_dict(result),
            "register": control_result_dict(register_result),
        }

    @app.post("/sip-trunks/{trunk_id}/connect")
    def connect_sip_trunk(trunk_id: str) -> dict[str, Any]:
        if sip_trunks is None:
            raise HTTPException(status_code=503, detail="SIP trunk registry is not configured")
        try:
            trunk = sip_trunks.set_enabled(trunk_id, True)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        if trunk is None:
            raise HTTPException(status_code=404, detail=f"SIP trunk not found: {trunk_id}")
        reload_result = reload_asterisk_pjsip()
        register_result = register_trunk(trunk)
        return {
            "trunk": trunk.redacted_dict(),
            "reload": control_result_dict(reload_result),
            "register": control_result_dict(register_result),
        }

    @app.post("/sip-trunks/{trunk_id}/disconnect")
    def disconnect_sip_trunk(trunk_id: str) -> dict[str, Any]:
        if sip_trunks is None:
            raise HTTPException(status_code=503, detail="SIP trunk registry is not configured")
        try:
            existing = sip_trunks.get(trunk_id)
            if existing is None:
                raise HTTPException(status_code=404, detail=f"SIP trunk not found: {trunk_id}")
            unregister_result = unregister_trunk(existing) if existing.enabled else None
            trunk = sip_trunks.set_enabled(trunk_id, False)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        reload_result = reload_asterisk_pjsip()
        return {
            "trunk": trunk.redacted_dict() if trunk is not None else None,
            "unregister": control_result_dict(unregister_result),
            "reload": control_result_dict(reload_result),
        }

    @app.delete("/sip-trunks/{trunk_id}")
    def delete_sip_trunk(trunk_id: str) -> dict[str, Any]:
        if sip_trunks is None:
            raise HTTPException(status_code=503, detail="SIP trunk registry is not configured")
        try:
            existing = sip_trunks.get(trunk_id)
            if existing is None:
                raise HTTPException(status_code=404, detail=f"SIP trunk not found: {trunk_id}")
            unregister_result = unregister_trunk(existing) if existing.enabled else None
            removed = sip_trunks.delete(trunk_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        reload_result = reload_asterisk_pjsip()
        return {
            "trunk": removed.redacted_dict() if removed is not None else None,
            "unregister": control_result_dict(unregister_result),
            "reload": control_result_dict(reload_result),
        }

    @app.get("/events")
    def list_events(after: int = 0, call_id: str | None = None, limit: int = 200) -> dict[str, Any]:
        result = [event_to_dict(event) for event in events.list_events(after=after, call_id=call_id, limit=validated_limit(limit))]
        return {"events": result}

    @app.get("/events/catalog")
    def list_event_catalog() -> dict[str, Any]:
        return {"events": event_catalog()}

    @app.get("/metrics")
    def metrics(call_id: str | None = None) -> dict[str, Any]:
        return summarize_metrics(events.list_events(call_id=call_id, limit=1000))

    @app.get("/observability/timeline")
    def observability_timeline(
        after: int = 0,
        call_id: str | None = None,
        workspace_id: str | None = None,
        voicebot_id: str | None = None,
        session_id: str | None = None,
        limit: int = 1000,
    ) -> dict[str, Any]:
        return build_timeline(
            events.list_events(
                after=after,
                call_id=call_id,
                workspace_id=workspace_id,
                voicebot_id=voicebot_id,
                session_id=session_id,
                limit=validated_limit(limit),
            )
        )

    @app.post("/observability/evaluate")
    def observability_evaluate(request: ConversationEvaluationRequest) -> dict[str, Any]:
        return evaluate_conversation(
            events.list_events(
                after=request.after,
                call_id=request.call_id,
                workspace_id=request.workspace_id,
                voicebot_id=request.voicebot_id,
                session_id=request.session_id,
                limit=validated_limit(request.limit),
            ),
            ConversationExpectation(
                must_include_event_types=tuple(request.must_include_event_types),
                max_duplicate_agent_responses=request.max_duplicate_agent_responses,
                require_final_agent_response=request.require_final_agent_response,
            ),
        )

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
        limit = validated_limit(limit)
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

    @app.post("/agent/tasks/renew")
    def renew_agent_tasks(request: AgentTaskRenewRequest) -> dict[str, Any]:
        renewed_event_ids = tracker.renew_many(request.event_ids, request.owner, request.ttl_seconds)
        for event_id in renewed_event_ids:
            source_event = events.get_event(event_id)
            if source_event is None:
                continue
            events.append(
                source_event.call_id,
                "agent_task_renewed",
                {
                    "task_event_id": event_id,
                    "owner": request.owner,
                    "ttl_seconds": request.ttl_seconds,
                },
            )
        return {"renewed_event_ids": renewed_event_ids, "owner": request.owner}

    @app.get("/agent/tasks/status")
    def agent_task_status(owner: str | None = None) -> dict[str, Any]:
        return tracker.snapshot(owner=owner)

    @app.get("/agent/tasks/summary")
    def agent_task_summary(
        after: int = 0,
        call_id: str | None = None,
        owner: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        limit = validated_limit(limit)
        active_call_ids = set(registry.active_call_ids())
        task_events = [
            event
            for event in events.list_events(after=after, limit=1000, call_id=call_id)
            if event.type == "agent_response_requested"
        ]
        tasks = []
        counts: dict[str, int] = {}
        for event in task_events:
            state = tracker.task_state(event.id, active=event.call_id in active_call_ids)
            if owner is not None and state.get("state") == "claimed" and state.get("owner") != owner:
                continue
            entry = {
                "event": event_to_dict(event),
                **state,
            }
            tasks.append(entry)
            state_name = str(state["state"])
            counts[state_name] = counts.get(state_name, 0) + 1
        return {
            "tasks": tasks[:limit],
            "counts": counts,
            "active_calls": sorted(active_call_ids),
        }

    @app.get("/subagent/tasks")
    def subagent_tasks(workspace_id: str | None = None, session_id: str | None = None) -> dict[str, Any]:
        if subagent_coordinator is None:
            raise HTTPException(status_code=503, detail="Subagent coordinator is not configured")
        tasks = subagent_coordinator.store.list(workspace_id=workspace_id, session_id=session_id)
        return {"tasks": [subagent_task_to_dict(task) for task in tasks]}

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
        try:
            event = await asyncio.to_thread(
                session.submit_agent_response,
                AgentResponse(
                    call_id=call_id,
                    text=request.text,
                    response_to_event_id=request.response_to_event_id,
                ),
            )
        except Exception as exc:
            tracker.mark_responded(request.response_to_event_id)
            failed = events.append(
                call_id,
                "agent_response_dropped",
                {
                    "reason": "playback_failed",
                    "error": str(exc),
                    "response_to_event_id": request.response_to_event_id,
                },
            )
            await hub.broadcast(failed)
            return {"event": event_to_dict(failed), "ok": False}
        tracker.mark_responded(request.response_to_event_id)
        await hub.broadcast(event)
        return {"event": event_to_dict(event), "ok": True}

    @app.get("/calls/{call_id}/transcript")
    def call_transcript(call_id: str, after: Any = 0, limit: Any = 200) -> dict[str, Any]:
        return {
            "call_id": call_id,
            "events": transcripts.read(
                call_id,
                after=optional_int_value(after, "after", 0),
                limit=validated_limit(optional_int_value(limit, "limit", 200)),
            ),
        }

    @app.get("/transcripts")
    def list_transcripts() -> dict[str, Any]:
        return {"call_ids": transcripts.list_call_ids()}

    @app.get("/transcripts/summary")
    def transcript_summaries(after_call_id: str | None = None, limit: Any = 200) -> dict[str, Any]:
        return {
            "transcripts": transcripts.summaries(
                after_call_id=after_call_id,
                limit=validated_limit(optional_int_value(limit, "limit", 200)),
            )
        }

    @app.get("/transcripts/stats")
    def transcript_stats(after_call_id: str | None = None, limit: Any = 200) -> dict[str, Any]:
        return transcripts.stats(
            after_call_id=after_call_id,
            limit=validated_limit(optional_int_value(limit, "limit", 200)),
        )

    @app.post("/calls/{call_id}/control")
    async def call_control(call_id: str, request: CallControlRequest) -> dict[str, Any]:
        requested = events.append(call_id, "call_control_requested", request.model_dump())
        active_session = registry.get(call_id)
        active_snapshot = active_session.snapshot() if active_session is not None else None
        transport = active_snapshot.get("transport") if isinstance(active_snapshot, dict) else None

        if transport == "webrtc":
            if request.action == "hangup":
                closed = False
                if webrtc is not None:
                    closed = await webrtc.close_call(call_id)
                elif active_session is not None:
                    active_session.stop()
                    registry.remove(call_id)
                    closed = True
                completed = events.append(
                    call_id,
                    "call_control_completed",
                    {
                        "action": request.action,
                        "ok": closed,
                        "message": "WebRTC call closed" if closed else "WebRTC call not found",
                        "request_event_id": requested.id,
                    },
                )
                tracker.mark_responded(request.response_to_event_id)
                await hub.broadcast(completed)
                return {"event": event_to_dict(completed)}

            completed = events.append(
                call_id,
                "call_control_completed",
                {
                    "action": request.action,
                    "ok": False,
                    "message": f"{request.action} is not supported for WebRTC calls yet",
                    "request_event_id": requested.id,
                },
            )
            tracker.mark_responded(request.response_to_event_id)
            await hub.broadcast(completed)
            return {"event": event_to_dict(completed)}

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

        try:
            if request.action == "hangup":
                result = asterisk.hangup(call_id)
            elif request.action == "transfer" and request.target:
                result = asterisk.transfer(call_id, validated_transfer_target(request.target))
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
                result = asterisk.send_dtmf(call_id, validated_dtmf_digit(request.digit))
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
        except HTTPException:
            raise
        except Exception as exc:
            result = ControlResult(False, f"Asterisk AMI request failed: {exc}")

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

    def reload_asterisk_pjsip():
        return safe_asterisk_action(lambda: asterisk.reload_pjsip())

    def register_trunk(trunk: SipTrunk):
        return safe_asterisk_action(lambda: asterisk.send_register(trunk.registration_name))

    def unregister_trunk(trunk: SipTrunk):
        return safe_asterisk_action(lambda: asterisk.send_unregister(trunk.registration_name))

    def safe_asterisk_action(action):
        if asterisk is None:
            return None
        try:
            return action()
        except OSError as exc:
            return ControlResult(False, f"Asterisk AMI request failed: {exc}")

    def control_result_dict(result) -> dict[str, Any] | None:
        if result is None:
            return None
        return {"ok": result.ok, "message": result.message}

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
        target = validated_transfer_target(require_arg(args, "target"))
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
        digit = validated_dtmf_digit(require_arg(args, "digit"))
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

    async def tool_create_flowhunt_project_issue(args: dict[str, Any]) -> dict[str, Any]:
        call_id = require_arg(args, "call_id")
        project_id = str(args.get("project_id") or runtime_settings.flowhunt_project_id)
        title = str(require_arg(args, "title"))
        description = str(require_arg(args, "description"))
        response_to_event_id = args.get("response_to_event_id")
        duplicate = existing_flowhunt_request(call_id, response_to_event_id)
        if duplicate is not None:
            tracker.mark_responded(response_to_event_id)
            return {
                "event": event_to_dict(duplicate),
                "ok": True,
                "message": "A FlowHunt colleague is already checking this request.",
                "duplicate": True,
            }
        if looks_like_vague_colleague_issue(title, description):
            return {
                "ok": False,
                "message": "The colleague issue was not created because the request is only a vague topic list. Ask the caller for the specific question first.",
            }
        if runtime_settings.flowhunt_complex_backend == "flow":
            return await tool_invoke_flowhunt_flow({**args, "message": description})
        await speak_tool_progress(
            call_id,
            "I will ask a colleague to check that and come back with the result.",
        )
        tracker.mark_responded(response_to_event_id)
        requested = events.append(
            call_id,
            "flowhunt_issue_created",
            {
                "project_id": project_id,
                "title": title,
                "response_to_event_id": response_to_event_id,
            },
        )
        await hub.broadcast(requested)
        client = FlowHuntClient(
            api_key=runtime_settings.flowhunt_api_key,
            workspace_id=runtime_settings.flowhunt_workspace_id,
            base_url=runtime_settings.flowhunt_base_url,
            timeout=runtime_settings.flowhunt_timeout,
        )
        result = await run_flowhunt_issue_with_progress(
            client,
            project_id,
            title,
            description,
            {"call_id": call_id, "response_to_event_id": response_to_event_id},
            runtime_settings.flowhunt_issue_wait_seconds,
            runtime_settings.flowhunt_issue_poll_interval_seconds,
            runtime_settings.flowhunt_progress_update_seconds,
            call_id,
            response_to_event_id=response_to_event_id,
        )
        result_event_type = "flowhunt_issue_updated" if result.data.get("pending") else "flowhunt_issue_completed"
        completed = events.append(
            call_id,
            result_event_type,
            {
                "ok": result.ok,
                "message": result.message,
                "project_id": project_id,
                "request_event_id": requested.id,
                "response_to_event_id": response_to_event_id,
                "data": result.data,
            },
        )
        await hub.broadcast(completed)
        if result.data.get("pending") and result.data.get("issue_id"):
            asyncio.create_task(
                watch_flowhunt_issue_until_complete(
                    client,
                    project_id,
                    str(result.data["issue_id"]),
                    call_id,
                    response_to_event_id,
                    runtime_settings.flowhunt_issue_background_wait_seconds,
                    runtime_settings.flowhunt_issue_poll_interval_seconds,
                    runtime_settings.flowhunt_progress_update_seconds,
                )
            )
        return {"event": event_to_dict(completed), "ok": result.ok, "message": result.message}

    async def tool_invoke_flowhunt_flow(args: dict[str, Any]) -> dict[str, Any]:
        call_id = require_arg(args, "call_id")
        flow_id = str(args.get("flow_id") or runtime_settings.flowhunt_flow_id)
        message = str(require_arg(args, "message"))
        response_to_event_id = args.get("response_to_event_id")
        duplicate = existing_flowhunt_request(call_id, response_to_event_id)
        if duplicate is not None:
            tracker.mark_responded(response_to_event_id)
            return {
                "event": event_to_dict(duplicate),
                "ok": True,
                "message": "A FlowHunt colleague is already checking this request.",
                "duplicate": True,
            }
        await speak_tool_progress(
            call_id,
            "I will ask a FlowHunt colleague to check that and come back with the result.",
        )
        tracker.mark_responded(response_to_event_id)
        invoked = events.append(
            call_id,
            "flowhunt_flow_invoked",
            {
                "flow_id": flow_id,
                "response_to_event_id": response_to_event_id,
            },
        )
        await hub.broadcast(invoked)
        if (
            subagent_coordinator is not None
            and subagent_lifecycle is not None
            and runtime_settings.flowhunt_workspace_id
            and "flowhunt_flow" in subagent_coordinator.providers
        ):
            task = subagent_coordinator.request(
                SubagentTaskRequest(
                    workspace_id=runtime_settings.flowhunt_workspace_id,
                    session_id=call_id,
                    request_event_id=invoked.id,
                    provider="flowhunt_flow",
                    input_text=message,
                    dedupe_key=str(response_to_event_id or invoked.id),
                    metadata={
                        "flow_id": flow_id,
                        "response_to_event_id": response_to_event_id,
                    },
                )
            )
            scheduled = subagent_lifecycle.schedule(task)
            return {
                "event": event_to_dict(invoked),
                "task": subagent_task_to_dict(scheduled),
                "ok": scheduled.status != "failed",
                "message": "A FlowHunt colleague is working on the request.",
            }
        client = FlowHuntClient(
            api_key=runtime_settings.flowhunt_api_key,
            workspace_id=runtime_settings.flowhunt_workspace_id,
            base_url=runtime_settings.flowhunt_base_url,
            timeout=runtime_settings.flowhunt_timeout,
        )
        result = await asyncio.to_thread(
            client.invoke_flow_and_wait,
            flow_id,
            message,
            runtime_settings.flowhunt_flow_wait_seconds,
            runtime_settings.flowhunt_flow_poll_interval_seconds,
        )
        result_event_type = "flowhunt_flow_updated" if result.data.get("pending") else "flowhunt_flow_completed"
        completed = events.append(
            call_id,
            result_event_type,
            {
                "ok": result.ok,
                "message": result.message,
                "flow_id": flow_id,
                "session_id": extract_session_id(result.data),
                "request_event_id": invoked.id,
                "response_to_event_id": response_to_event_id,
                "data": result.data,
            },
        )
        await hub.broadcast(completed)
        if result.data.get("pending") and result.data.get("task_id"):
            asyncio.create_task(
                watch_flowhunt_flow_task_until_complete(
                    client,
                    flow_id,
                    str(result.data["task_id"]),
                    call_id,
                    response_to_event_id,
                    runtime_settings.flowhunt_issue_background_wait_seconds,
                    runtime_settings.flowhunt_flow_poll_interval_seconds,
                )
            )
        elif result.data.get("pending") and extract_session_id(result.data):
            asyncio.create_task(
                watch_flowhunt_flow_until_complete(
                    client,
                    str(extract_session_id(result.data)),
                    call_id,
                    response_to_event_id,
                    flow_id,
                    runtime_settings.flowhunt_issue_background_wait_seconds,
                    runtime_settings.flowhunt_flow_poll_interval_seconds,
                )
            )
        else:
            await request_communication_agent(
                call_id,
                "colleague_result",
                f"A FlowHunt colleague finished checking the caller request. Result: {result.message}",
                response_to_event_id,
                flow_id=flow_id,
                session_id=extract_session_id(result.data),
                ok=result.ok,
                source_event_id=completed.id,
                data=result.data,
            )
        return {"event": event_to_dict(completed), "ok": result.ok, "message": result.message}

    def existing_flowhunt_request(call_id: str, response_to_event_id: Any) -> VoicebotEvent | None:
        if response_to_event_id is None:
            return None
        try:
            response_event_id = int(response_to_event_id)
        except (TypeError, ValueError):
            return None
        for event in reversed(events.list_events(call_id=call_id, limit=1000)):
            if event.type not in {
                "flowhunt_flow_invoked",
                "flowhunt_flow_updated",
                "flowhunt_flow_completed",
                "flowhunt_issue_created",
                "flowhunt_issue_updated",
                "flowhunt_issue_completed",
            }:
                continue
            try:
                event_response_id = int(event.data.get("response_to_event_id"))
            except (TypeError, ValueError):
                continue
            if event_response_id == response_event_id:
                return event
        return None

    async def speak_tool_progress(call_id: str, text: str) -> None:
        session = registry.get(call_id)
        if session is None:
            return
        try:
            await asyncio.to_thread(session.submit_agent_response, AgentResponse(call_id=call_id, text=text))
        except Exception as exc:
            events.append(call_id, "agent_response_dropped", {"reason": "progress_playback_failed", "error": str(exc)})

    async def request_communication_agent(
        call_id: str,
        reason: str,
        text: str,
        response_to_event_id: int | None,
        **data: Any,
    ) -> None:
        if registry.get(call_id) is None:
            return
        requested = events.append(
            call_id,
            "agent_response_requested",
            {
                "reason": reason,
                "text": text,
                "response_to_event_id": response_to_event_id,
                **data,
            },
        )
        await hub.broadcast(requested)

    async def notify_subagent_terminal_task(task: SubagentTask) -> None:
        if registry.get(task.session_id) is None:
            return
        if task.status == "completed":
            result = task.result
            message = result.content if result and result.content else result.summary if result else "The delegated task completed."
            await request_communication_agent(
                task.session_id,
                "colleague_result",
                f"A colleague finished checking the caller request. Result: {message}",
                task.request_event_id,
                subagent_task_id=task.task_id,
                provider=task.provider,
                external_task_id=task.external_task_id,
                ok=True,
                data=task.clean_result_context(),
            )
            return
        if task.status in {"failed", "timed_out"}:
            await request_communication_agent(
                task.session_id,
                "colleague_result",
                f"A delegated colleague task could not finish: {task.error or task.status}.",
                task.request_event_id,
                subagent_task_id=task.task_id,
                provider=task.provider,
                external_task_id=task.external_task_id,
                ok=False,
                data=task.clean_result_context(),
            )

    async def run_subagent_lifecycle_loop(stop: asyncio.Event) -> None:
        if subagent_lifecycle is None:
            return
        while not stop.is_set():
            try:
                changed = await asyncio.to_thread(subagent_lifecycle.tick)
                pending_broadcasts = list(subagent_terminal_events)
                subagent_terminal_events.clear()
                for event in pending_broadcasts:
                    await hub.broadcast(event)
                for task in changed:
                    if task.is_terminal() and task.terminal_event_emitted_at:
                        await notify_subagent_terminal_task(task)
            except Exception as exc:
                events.append("system", "system", {"component": "subagent_lifecycle", "error": str(exc)})
            try:
                await asyncio.wait_for(stop.wait(), timeout=max(0.2, runtime_settings.subagent_task_poll_loop_seconds))
            except asyncio.TimeoutError:
                pass

    async def run_flowhunt_issue_with_progress(
        client: FlowHuntClient,
        project_id: str,
        title: str,
        description: str,
        metadata: dict[str, Any],
        wait_seconds: float,
        poll_interval_seconds: float,
        progress_update_seconds: float,
        call_id: str,
        response_to_event_id: int | None = None,
    ) -> FlowHuntResult:
        created = await asyncio.to_thread(client.create_project_issue, project_id, title, description, metadata)
        if not created.ok:
            return created

        issue_id = extract_issue_id(created.data.get("response")) or extract_issue_id(created.data)
        if not issue_id:
            return FlowHuntResult(True, created.message or "FlowHunt project issue was created.", created.data)

        deadline = asyncio.get_running_loop().time() + max(0.0, wait_seconds)
        next_progress = asyncio.get_running_loop().time() + max(1.0, progress_update_seconds)
        latest = created
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(max(0.2, poll_interval_seconds))
            latest = await asyncio.to_thread(client.get_project_issue, project_id, issue_id)
            if not latest.ok:
                return latest
            response = latest.data.get("response")
            state = extract_issue_state(response).lower()
            if is_terminal_issue_state(state):
                break
            if extract_issue_result(response):
                break
            now = asyncio.get_running_loop().time()
            if now >= next_progress:
                update = extract_issue_updates(response)
                state_text = extract_issue_state(response)
                updated = events.append(
                    call_id,
                    "flowhunt_issue_updated",
                    {
                        "ok": True,
                        "message": update or (f"Current status is {state_text}." if state_text else "Still in progress."),
                        "project_id": project_id,
                        "issue_id": issue_id,
                        "response_to_event_id": response_to_event_id,
                        "data": latest.data,
                    },
                )
                await hub.broadcast(updated)
                next_progress = now + max(1.0, progress_update_seconds)

        response = latest.data.get("response")
        result = extract_issue_result(response)
        state = extract_issue_state(response)
        update = extract_issue_updates(response)
        if result:
            return FlowHuntResult(True, result, latest.data)
        if state and is_terminal_issue_state(state):
            ok = state not in {"failed", "error", "cancelled", "canceled", "human_input_needed"}
            message = update or f"The colleague task finished with status {state}."
            return FlowHuntResult(ok, message, latest.data)
        data = dict(latest.data)
        data["pending"] = True
        data["issue_id"] = issue_id
        if update:
            data["latest_update"] = update
        if state:
            if update:
                return FlowHuntResult(True, f"The colleague is still working on it. Current status is {state}. Latest update: {update}", data)
            return FlowHuntResult(True, f"The colleague is still working on it. Current status is {state}.", data)
        if update:
            return FlowHuntResult(True, f"The colleague is still working on it. Latest update: {update}", data)
        return FlowHuntResult(True, "The colleague is still working on it. I will keep watching for the result.", data)

    async def watch_flowhunt_issue_until_complete(
        client: FlowHuntClient,
        project_id: str,
        issue_id: str,
        call_id: str,
        response_to_event_id: int | None,
        wait_seconds: float,
        poll_interval_seconds: float,
        progress_update_seconds: float,
    ) -> None:
        deadline = asyncio.get_running_loop().time() + max(0.0, wait_seconds)
        next_progress = asyncio.get_running_loop().time() + max(1.0, progress_update_seconds)
        last_progress_message = ""
        while asyncio.get_running_loop().time() < deadline:
            if registry.get(call_id) is None:
                return
            await asyncio.sleep(max(0.2, poll_interval_seconds))
            latest = await asyncio.to_thread(client.get_project_issue, project_id, issue_id)
            if not latest.ok:
                completed = events.append(
                    call_id,
                    "flowhunt_issue_completed",
                    {
                        "ok": False,
                        "message": latest.message,
                        "project_id": project_id,
                        "issue_id": issue_id,
                        "response_to_event_id": response_to_event_id,
                        "data": latest.data,
                    },
                )
                await hub.broadcast(completed)
                await request_communication_agent(
                    call_id,
                    "colleague_result",
                    f"A colleague task failed while checking the caller request: {latest.message}",
                    response_to_event_id,
                    project_id=project_id,
                    issue_id=issue_id,
                    ok=False,
                    source_event_id=completed.id,
                    data=latest.data,
                )
                return

            response = latest.data.get("response")
            result = extract_issue_result(response)
            update = extract_issue_updates(response)
            state = extract_issue_state(response).lower()
            if result or is_terminal_issue_state(state):
                message = result or update or f"The colleague task finished with status {state}."
                completed = events.append(
                    call_id,
                    "flowhunt_issue_completed",
                    {
                        "ok": state not in {"failed", "error", "cancelled", "canceled", "human_input_needed"},
                        "message": message,
                        "project_id": project_id,
                        "issue_id": issue_id,
                        "response_to_event_id": response_to_event_id,
                        "data": latest.data,
                    },
                )
                await hub.broadcast(completed)
                await request_communication_agent(
                    call_id,
                    "colleague_result",
                    f"A colleague finished checking the caller request. Result: {message}",
                    response_to_event_id,
                    project_id=project_id,
                    issue_id=issue_id,
                    ok=state not in {"failed", "error", "cancelled", "canceled", "human_input_needed"},
                    source_event_id=completed.id,
                    data=latest.data,
                )
                return

            now = asyncio.get_running_loop().time()
            if now >= next_progress:
                message = update or "The colleague task is still in progress."
                if message == last_progress_message:
                    next_progress = now + max(1.0, progress_update_seconds)
                    continue
                last_progress_message = message
                updated = events.append(
                    call_id,
                    "flowhunt_issue_updated",
                    {
                        "ok": True,
                        "message": message,
                        "project_id": project_id,
                        "issue_id": issue_id,
                        "response_to_event_id": response_to_event_id,
                        "data": latest.data,
                    },
                )
                await hub.broadcast(updated)
                await request_communication_agent(
                    call_id,
                    "colleague_progress",
                    f"A colleague is still checking the caller request. Current update: {message}",
                    response_to_event_id,
                    project_id=project_id,
                    issue_id=issue_id,
                    source_event_id=updated.id,
                    data=latest.data,
                )
                next_progress = now + max(1.0, progress_update_seconds)

    async def watch_flowhunt_flow_until_complete(
        client: FlowHuntClient,
        session_id: str,
        call_id: str,
        response_to_event_id: int | None,
        flow_id: str,
        wait_seconds: float,
        poll_interval_seconds: float,
    ) -> None:
        deadline = asyncio.get_running_loop().time() + max(0.0, wait_seconds)
        from_timestamp = "0"
        seen_event_ids: set[str] = set()
        while asyncio.get_running_loop().time() < deadline:
            if registry.get(call_id) is None:
                return
            await asyncio.sleep(max(0.2, poll_interval_seconds))
            latest = await asyncio.to_thread(client.poll_flow_events, session_id, from_timestamp)
            events_data = latest.data.get("events") if isinstance(latest.data, dict) else []
            if isinstance(events_data, list):
                new_events = []
                for event_data in events_data:
                    event_id = str(event_data.get("event_id") or "")
                    if event_id and event_id in seen_event_ids:
                        continue
                    if event_id:
                        seen_event_ids.add(event_id)
                    new_events.append(event_data)
                if new_events:
                    from_timestamp = max(
                        [
                            str(event_data.get("created_at_timestamp") or event_data.get("created_at") or from_timestamp)
                            for event_data in new_events
                        ]
                    )
            if latest.ok and not latest.data.get("pending"):
                completed = events.append(
                    call_id,
                    "flowhunt_flow_completed",
                    {
                        "ok": True,
                        "message": latest.message,
                        "flow_id": flow_id,
                        "session_id": session_id,
                        "response_to_event_id": response_to_event_id,
                        "data": latest.data,
                    },
                )
                await hub.broadcast(completed)
                await request_communication_agent(
                    call_id,
                    "colleague_result",
                    f"A FlowHunt colleague finished checking the caller request. Result: {latest.message}",
                    response_to_event_id,
                    flow_id=flow_id,
                    session_id=session_id,
                    ok=True,
                    source_event_id=completed.id,
                    data=latest.data,
                )
                return

    async def watch_flowhunt_flow_task_until_complete(
        client: FlowHuntClient,
        flow_id: str,
        task_id: str,
        call_id: str,
        response_to_event_id: int | None,
        wait_seconds: float,
        poll_interval_seconds: float,
    ) -> None:
        deadline = asyncio.get_running_loop().time() + max(0.0, wait_seconds)
        while asyncio.get_running_loop().time() < deadline:
            if registry.get(call_id) is None:
                return
            await asyncio.sleep(max(0.2, poll_interval_seconds))
            latest = await asyncio.to_thread(client.get_flow_task, flow_id, task_id)
            if not latest.ok:
                completed = events.append(
                    call_id,
                    "flowhunt_flow_completed",
                    {
                        "ok": False,
                        "message": latest.message,
                        "flow_id": flow_id,
                        "task_id": task_id,
                        "response_to_event_id": response_to_event_id,
                        "data": latest.data,
                    },
                )
                await hub.broadcast(completed)
                await request_communication_agent(
                    call_id,
                    "colleague_result",
                    f"A FlowHunt colleague task failed while checking the caller request: {latest.message}",
                    response_to_event_id,
                    flow_id=flow_id,
                    task_id=task_id,
                    ok=False,
                    source_event_id=completed.id,
                    data=latest.data,
                )
                return
            task = latest.data.get("response")
            result = extract_flow_task_result(task)
            if result or is_flow_task_terminal(task):
                ok = bool(result)
                message = result or extract_flow_task_error(task) or "FlowHunt flow finished without a result."
                completed = events.append(
                    call_id,
                    "flowhunt_flow_completed",
                    {
                        "ok": ok,
                        "message": message,
                        "flow_id": flow_id,
                        "task_id": task_id,
                        "response_to_event_id": response_to_event_id,
                        "data": latest.data,
                    },
                )
                await hub.broadcast(completed)
                await request_communication_agent(
                    call_id,
                    "colleague_result",
                    f"A FlowHunt colleague finished checking the caller request. Result: {message}",
                    response_to_event_id,
                    flow_id=flow_id,
                    task_id=task_id,
                    ok=ok,
                    source_event_id=completed.id,
                    data=latest.data,
                )
                return

    def tool_get_transcript(args: dict[str, Any]) -> dict[str, Any]:
        call_id = require_arg(args, "call_id")
        return call_transcript(
            call_id,
            after=optional_int_arg(args, "after", 0),
            limit=validated_limit(optional_int_arg(args, "limit", 200)),
        )

    def tool_list_transcripts(args: dict[str, Any]) -> dict[str, Any]:
        return list_transcripts()

    def tool_list_transcript_summaries(args: dict[str, Any]) -> dict[str, Any]:
        return transcript_summaries(
            after_call_id=args.get("after_call_id"),
            limit=validated_limit(optional_int_arg(args, "limit", 200)),
        )

    def tool_get_transcript_stats(args: dict[str, Any]) -> dict[str, Any]:
        return transcript_stats(
            after_call_id=args.get("after_call_id"),
            limit=validated_limit(optional_int_arg(args, "limit", 200)),
        )

    def tool_get_events(args: dict[str, Any]) -> dict[str, Any]:
        return list_events(
            after=optional_int_arg(args, "after", 0),
            call_id=args.get("call_id"),
            limit=validated_limit(optional_int_arg(args, "limit", 200)),
        )

    def tool_get_metrics(args: dict[str, Any]) -> dict[str, Any]:
        return metrics(call_id=args.get("call_id"))

    def tool_get_active_calls(args: dict[str, Any]) -> dict[str, Any]:
        return {"active_calls": registry.active_call_ids()}

    def tool_get_call_state(args: dict[str, Any]) -> dict[str, Any]:
        call_id = require_arg(args, "call_id")
        return call_state(call_id)

    def tool_get_runtime_config(args: dict[str, Any]) -> dict[str, Any]:
        return config()

    def tool_get_agent_task_status(args: dict[str, Any]) -> dict[str, Any]:
        return agent_task_status(owner=args.get("owner"))

    def tool_get_agent_task_summary(args: dict[str, Any]) -> dict[str, Any]:
        return agent_task_summary(
            after=optional_int_arg(args, "after", 0),
            call_id=args.get("call_id"),
            owner=args.get("owner"),
            limit=validated_limit(optional_int_arg(args, "limit", 200)),
        )

    tool_executor.register("say", tool_say)
    tool_executor.register("hangup_call", tool_hangup_call)
    tool_executor.register("transfer_call", tool_transfer_call)
    tool_executor.register("send_dtmf", tool_send_dtmf)
    tool_executor.register("stop_playback", tool_stop_playback)
    tool_executor.register("invoke_flowhunt_flow", tool_invoke_flowhunt_flow)
    tool_executor.register("create_flowhunt_project_issue", tool_create_flowhunt_project_issue)
    tool_executor.register("list_transcripts", tool_list_transcripts)
    tool_executor.register("list_transcript_summaries", tool_list_transcript_summaries)
    tool_executor.register("get_transcript_stats", tool_get_transcript_stats)
    tool_executor.register("get_transcript", tool_get_transcript)
    tool_executor.register("get_events", tool_get_events)
    tool_executor.register("get_metrics", tool_get_metrics)
    tool_executor.register("get_active_calls", tool_get_active_calls)
    tool_executor.register("get_call_state", tool_get_call_state)
    tool_executor.register("get_runtime_config", tool_get_runtime_config)
    tool_executor.register("get_agent_task_status", tool_get_agent_task_status)
    tool_executor.register("get_agent_task_summary", tool_get_agent_task_summary)

    return app


def require_arg(args: dict[str, Any], name: str) -> Any:
    value = args.get(name)
    if value is None or value == "":
        raise HTTPException(status_code=400, detail=f"missing required tool argument: {name}")
    return value


def validated_limit(limit: int, *, maximum: int = 1000) -> int:
    if limit < 1:
        raise HTTPException(status_code=400, detail="limit must be at least 1")
    if limit > maximum:
        raise HTTPException(status_code=400, detail=f"limit must be at most {maximum}")
    return limit


def optional_int_arg(args: dict[str, Any], name: str, default: int) -> int:
    return optional_int_value(args.get(name, default), name, default)


def optional_int_value(value: Any, name: str, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{name} must be an integer") from None


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


def looks_like_vague_colleague_issue(title: str, description: str) -> bool:
    text = f"{title}\n{description}".lower()
    markers = (
        "caller mentioned a range of topics",
        "caller referenced multiple technologies",
        "general support request",
        "various technologies",
        "may have questions or requests",
        "need comprehensive support",
    )
    if not any(marker in text for marker in markers):
        return False
    concrete_markers = ("how ", "what ", "why ", "when ", "where ", "check ", "count ", "create ", "transfer ", "hang up")
    return not any(marker in text for marker in concrete_markers)


WEBRTC_TEST_PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>FlowHunt Voicebot WebRTC Test</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 2rem; max-width: 780px; line-height: 1.45; }
    button { font: inherit; margin-right: .5rem; padding: .45rem .75rem; }
    pre { background: #111; color: #eee; padding: 1rem; overflow: auto; min-height: 12rem; }
  </style>
</head>
<body>
  <h1>FlowHunt Voicebot WebRTC Test</h1>
  <p>Click Start, allow microphone access, then speak. The bot audio is played by the browser.</p>
  <button id="start">Start call</button>
  <button id="stop" disabled>Stop call</button>
  <audio id="remote" autoplay playsinline controls></audio>
  <pre id="log"></pre>
  <script>
    const startButton = document.getElementById("start");
    const stopButton = document.getElementById("stop");
    const remoteAudio = document.getElementById("remote");
    const logNode = document.getElementById("log");
    let pc = null;
    let sessionId = null;
    let localStream = null;

    function log(message) {
      logNode.textContent += `${new Date().toISOString()} ${message}\\n`;
      logNode.scrollTop = logNode.scrollHeight;
    }

    startButton.onclick = async () => {
      startButton.disabled = true;
      try {
        pc = new RTCPeerConnection({iceServers: [{urls: "stun:stun.l.google.com:19302"}]});
        pc.onconnectionstatechange = () => log(`connectionState=${pc.connectionState}`);
        pc.ontrack = (event) => {
          log(`received remote ${event.track.kind} track`);
          remoteAudio.srcObject = event.streams[0] || new MediaStream([event.track]);
        };
        localStream = await navigator.mediaDevices.getUserMedia({
          audio: {
            channelCount: {ideal: 1},
            sampleRate: {ideal: 48000},
            sampleSize: {ideal: 16},
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true,
            latency: {ideal: 0.02}
          }
        });
        for (const track of localStream.getAudioTracks()) {
          track.contentHint = "speech";
          log(`local audio settings=${JSON.stringify(track.getSettings())}`);
          pc.addTrack(track, localStream);
        }
        const offer = await pc.createOffer();
        await pc.setLocalDescription(offer);
        await new Promise((resolve) => {
          if (pc.iceGatheringState === "complete") {
            resolve();
            return;
          }
          pc.onicegatheringstatechange = () => {
            if (pc.iceGatheringState === "complete") resolve();
          };
        });
        const response = await fetch("/webrtc/sessions", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            sdp: pc.localDescription.sdp,
            type: pc.localDescription.type,
            metadata: {client: "browser-test"}
          })
        });
        if (!response.ok) {
          throw new Error(await response.text());
        }
        const payload = await response.json();
        sessionId = payload.session_id;
        await pc.setRemoteDescription(payload.answer);
        stopButton.disabled = false;
        log(`started session=${sessionId} call=${payload.call_id}`);
      } catch (error) {
        log(`error: ${error}`);
        await stopCall();
        startButton.disabled = false;
      }
    };

    stopButton.onclick = async () => {
      await stopCall();
      startButton.disabled = false;
    };

    async function stopCall() {
      stopButton.disabled = true;
      if (sessionId) {
        try {
          await fetch(`/webrtc/sessions/${sessionId}`, {method: "DELETE"});
        } catch (error) {
          log(`delete failed: ${error}`);
        }
      }
      sessionId = null;
      if (localStream) {
        for (const track of localStream.getTracks()) track.stop();
      }
      localStream = null;
      if (pc) {
        pc.close();
      }
      pc = null;
      remoteAudio.srcObject = null;
      log("stopped");
    }
  </script>
</body>
</html>
"""
