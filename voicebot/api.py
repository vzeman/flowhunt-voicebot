from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, get_args

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, PlainTextResponse

from .agent_tasks import AgentTaskTracker
from .api_surface import (
    api_scope_violations,
    api_surface_by_area,
    api_surface_integrity_issues,
    api_surface_summary,
    prototype_endpoints,
    public_endpoints_are_workspace_scoped,
)
from .api_models import (
    AgentResponseRequest,
    AgentTaskClaimRequest,
    AgentTaskReleaseRequest,
    AgentTaskRenewRequest,
    AgentToolRequest,
    CallControlRequest,
    CompactContextRequest,
    ConversationEvaluationRequest,
    DrainRequest,
    IncomingSessionAdmissionRequest,
    MultimodalContentRequest,
    PlaybackInterruptRequest,
    ScalingAdmissionRequest,
    ScalingBackpressureRequest,
    ScalingWorkloadPlanRequest,
    SessionLeaseEnforceRequest,
    SessionLeaseReleaseRequest,
    SessionLeaseRequest,
    SipTrunkRequest,
    VoicebotAdminPatchRequest,
    VoicebotAdminRequest,
    VoicebotChannelPatchRequest,
    VoicebotChannelRequest,
    VoicebotProviderConfigRequest,
    VoicebotRuntimeConfigRequest,
    WorkerQueueClaimRequest,
    WorkerQueueEnqueueRequest,
    WorkerHeartbeatRequest,
    WorkerQueueItemRequest,
    WebRTCOfferRequest,
)
from .asterisk_control import AsteriskAMI, ControlResult
from .calls import AgentResponse, CallRegistry
from .config import Settings, redacted_settings
from .drain import DrainState, rollout_contract
from .event_catalog import event_catalog, event_catalog_integrity_issues
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
from .multimodal import (
    ContentDirection,
    Modality,
    ModalityCapabilities,
    MultimodalContent,
    MultimodalContextStore,
    validate_multimodal_content,
)
from .observability import ConversationExpectation, build_timeline, evaluate_conversation
from .pipeline_contract import pipeline_contract_payload
from .provider_catalog import _agent_capabilities, _stt_capabilities, _tts_capabilities, provider_catalog
from .provider_config import (
    ProviderChoice,
    ProviderConfigStore,
    SecretReference,
    VoicebotProviderConfig,
    provider_config_to_dict,
    provider_selection_plan,
    selection_plan_to_dict,
    validate_provider_config,
    validation_issue_to_dict,
)
from .scaling import (
    RoutingKey,
    WorkerInstance,
    WorkerQueueEnvelope,
    WorkerQueueStore,
    WorkerRegistry,
    WorkerRole,
    WorkloadProfile,
    WarmCapacityPolicy,
    WorkspaceBackpressure,
    admission_decision,
    autoscaling_signals,
    autoscaling_signals_prometheus,
    build_workload_plan,
    default_deployment_topology,
)
from .session_leases import SessionLeaseStore
from .sip_media_plane import sip_media_plane_payload
from .sip_trunks import SipTrunk, SipTrunkStore
from .storage_contracts import storage_contracts_payload
from .runtime_config import (
    VoicebotPromptConfig,
    VoicebotQuotaConfig,
    VoicebotRealtimeConfig,
    VoicebotRuntimeConfig,
    VoicebotRuntimeConfigStore,
    VoicebotSubagentConfig,
    runtime_config_to_dict,
)
from .routing_admission import IncomingSessionRequest, evaluate_incoming_session
from .subagents import SubagentCoordinator, SubagentTask, SubagentTaskRequest, subagent_task_to_dict
from .task_lifecycle import PollingPolicy, SubagentTaskLifecycleRunner, TaskLifecycleEventType
from .tool_executor import AgentToolExecutor
from .transcripts import TranscriptStore
from .transports import transport_catalog
from .tools import tool_definitions_json_schema, tool_definitions_legacy
from .webrtc import WebRTCSessionManager
from .webrtc_media_plane import webrtc_media_plane_payload
from .workspace_access import WorkspaceAccessPolicy, workspace_access_policy_from_settings
from .workspace_model import ChannelKind, ChannelResolver, VoicebotChannelBinding, VoicebotDefinition, VoicebotSessionStore, VoicebotStore

_MODALITIES = set(get_args(Modality))
_CONTENT_DIRECTIONS = set(get_args(ContentDirection))
_API_MULTIMODAL_CAPABILITIES = ModalityCapabilities(
    input=frozenset(_MODALITIES),
    output=frozenset(_MODALITIES),
)


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
    multimodal_contexts: MultimodalContextStore | None = None,
    provider_configs: ProviderConfigStore | None = None,
    worker_registry: WorkerRegistry | None = None,
    worker_queue: WorkerQueueStore | None = None,
    voicebots: VoicebotStore | None = None,
    channels: ChannelResolver | None = None,
    voicebot_sessions: VoicebotSessionStore | None = None,
    session_leases: SessionLeaseStore | None = None,
    workspace_policy: WorkspaceAccessPolicy | None = None,
    runtime_configs: VoicebotRuntimeConfigStore | None = None,
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
    drain_state = DrainState()
    multimodal_store = multimodal_contexts or MultimodalContextStore()
    provider_config_store = provider_configs or ProviderConfigStore()
    scaling_workers = worker_registry or WorkerRegistry()
    scaling_queue = worker_queue or WorkerQueueStore()
    scaling_backpressure = WorkspaceBackpressure(runtime_settings.scaling_backpressure_max_inflight)
    voicebot_store = voicebots or VoicebotStore()
    channel_resolver = channels or ChannelResolver()
    voicebot_session_store = voicebot_sessions or VoicebotSessionStore()
    session_lease_store = session_leases or SessionLeaseStore()
    runtime_config_store = runtime_configs or VoicebotRuntimeConfigStore()
    workspace_access_policy = workspace_policy or workspace_access_policy_from_settings(runtime_settings)
    subagent_terminal_events: list[VoicebotEvent] = []

    def require_workspace_access(workspace_id: str) -> None:
        try:
            workspace_access_policy.require_workspace(workspace_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from None

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
            storage_components={
                "events": events,
                "agent_tasks": tracker,
                "call_states": registry.state_store,
                "session_leases": session_lease_store,
                "worker_registry": scaling_workers,
                "voicebot_sessions": voicebot_session_store,
                "worker_queue": scaling_queue,
                **({"subagent_tasks": subagent_coordinator.store} if subagent_coordinator is not None else {}),
            },
            drain_state=drain_state.snapshot(),
        )

    @app.get("/health/liveness")
    def liveness() -> dict[str, Any]:
        return {"ok": True, "draining": drain_state.draining}

    @app.get("/operations/drain")
    def get_drain_state() -> dict[str, Any]:
        return {"drain": drain_state.snapshot(), "rollout": rollout_contract()}

    @app.post("/operations/drain/start")
    def start_drain(request: DrainRequest) -> dict[str, Any]:
        state = drain_state.start(request.reason)
        event = events.append("runtime", "runtime_draining_started", state)
        interrupted = []
        if request.interrupt_active_sessions:
            for snapshot in registry.snapshots():
                call_id = snapshot["call_id"]
                stopped = registry.stop(call_id)
                interrupted_event = events.append(
                    call_id,
                    "session_interrupted",
                    {
                        "reason": "runtime_draining",
                        "stopped": stopped,
                        "drain": state,
                        "workspace_id": (snapshot.get("route") or {}).get("workspace_id"),
                        "voicebot_id": (snapshot.get("route") or {}).get("voicebot_id"),
                    },
                )
                interrupted.append(event_to_dict(interrupted_event))
        events.append("runtime", "metrics", {"name": "runtime_draining", "value": 1.0, "reason": state["reason"]})
        return {"event_id": event.id, "drain": state, "interrupted": interrupted}

    @app.post("/operations/drain/stop")
    def stop_drain() -> dict[str, Any]:
        state = drain_state.stop()
        event = events.append("runtime", "runtime_draining_stopped", state)
        events.append("runtime", "metrics", {"name": "runtime_draining", "value": 0.0})
        return {"event_id": event.id, "drain": state}

    @app.get("/storage/contracts")
    def storage_contracts() -> dict[str, Any]:
        return storage_contracts_payload()

    @app.get("/pipeline/contract")
    def pipeline_contract() -> dict[str, Any]:
        return pipeline_contract_payload()

    @app.get("/calls")
    def list_calls() -> dict[str, Any]:
        return {"calls": registry.snapshots()}

    @app.get("/calls/state-store")
    def list_stored_call_states(active_only: bool = False) -> dict[str, Any]:
        return {"calls": registry.stored_snapshots(active_only=active_only)}

    @app.get("/calls/{call_id}")
    def call_state(call_id: str) -> dict[str, Any]:
        snapshot = registry.snapshot(call_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail=f"Active call not found: {call_id}")
        return snapshot

    @app.get("/calls/{call_id}/multimodal")
    def call_multimodal_context(call_id: str) -> dict[str, Any]:
        return multimodal_store.get(call_id).to_agent_context()

    @app.post("/calls/{call_id}/multimodal/parts")
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
        validation_issues = validate_multimodal_content(part, _API_MULTIMODAL_CAPABILITIES)
        if validation_issues:
            raise HTTPException(status_code=400, detail=[issue.to_dict() for issue in validation_issues])
        try:
            context = multimodal_store.add_part(
                call_id,
                part,
                workspace_id=request.workspace_id,
                voicebot_id=request.voicebot_id,
                session_id=request.session_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        event = events.append(
            call_id,
            "multimodal_content_added",
            {
                "workspace_id": request.workspace_id,
                "voicebot_id": request.voicebot_id,
                "session_id": request.session_id,
                "part": part.to_agent_part(),
                "part_count": len(context.parts),
            },
        )
        await hub.broadcast(event)
        return {"context": context.to_agent_context(), "event": event_to_dict(event)}

    @app.get("/providers")
    def providers() -> dict[str, Any]:
        return provider_catalog()

    @app.get("/api/surface")
    def api_surface() -> dict[str, Any]:
        return {
            "summary": api_surface_summary(),
            "areas": api_surface_by_area(),
            "public_endpoints_are_workspace_scoped": public_endpoints_are_workspace_scoped(),
            "scope_violations": api_scope_violations(),
            "integrity_issues": api_surface_integrity_issues(),
        }

    @app.get("/api/surface/prototypes")
    def api_surface_prototypes() -> dict[str, Any]:
        return {"endpoints": prototype_endpoints()}

    @app.get("/workspaces/{workspace_id}/voicebots")
    def list_workspace_voicebots(workspace_id: str) -> dict[str, Any]:
        require_workspace_access(workspace_id)
        return {
            "workspace_id": workspace_id,
            "voicebots": [voicebot.as_dict() for voicebot in voicebot_store.list(workspace_id)],
        }

    @app.post("/workspaces/{workspace_id}/voicebots")
    def create_workspace_voicebot(workspace_id: str, request: VoicebotAdminRequest) -> dict[str, Any]:
        require_workspace_access(workspace_id)
        try:
            voicebot = voicebot_store.create(
                VoicebotDefinition(
                    workspace_id=workspace_id,
                    voicebot_id=request.voicebot_id,
                    display_name=request.display_name,
                    enabled=request.enabled,
                    metadata=request.metadata,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"voicebot": voicebot.as_dict()}

    @app.get("/workspaces/{workspace_id}/voicebots/{voicebot_id}")
    def get_workspace_voicebot(workspace_id: str, voicebot_id: str) -> dict[str, Any]:
        require_workspace_access(workspace_id)
        voicebot = voicebot_store.get(workspace_id, voicebot_id)
        if voicebot is None:
            raise HTTPException(status_code=404, detail="Voicebot not found")
        return {"voicebot": voicebot.as_dict()}

    @app.patch("/workspaces/{workspace_id}/voicebots/{voicebot_id}")
    def patch_workspace_voicebot(
        workspace_id: str,
        voicebot_id: str,
        request: VoicebotAdminPatchRequest,
    ) -> dict[str, Any]:
        require_workspace_access(workspace_id)
        try:
            voicebot = voicebot_store.patch(
                workspace_id,
                voicebot_id,
                display_name=request.display_name,
                enabled=request.enabled,
                metadata=request.metadata,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="Voicebot not found") from None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"voicebot": voicebot.as_dict()}

    @app.delete("/workspaces/{workspace_id}/voicebots/{voicebot_id}")
    def delete_workspace_voicebot(workspace_id: str, voicebot_id: str) -> dict[str, Any]:
        require_workspace_access(workspace_id)
        voicebot = voicebot_store.delete(workspace_id, voicebot_id)
        if voicebot is None:
            raise HTTPException(status_code=404, detail="Voicebot not found")
        return {"voicebot": voicebot.as_dict(), "deleted": True}

    @app.get("/workspaces/{workspace_id}/voicebots/{voicebot_id}/channels")
    def list_voicebot_channels(workspace_id: str, voicebot_id: str) -> dict[str, Any]:
        require_workspace_access(workspace_id)
        return {
            "workspace_id": workspace_id,
            "voicebot_id": voicebot_id,
            "channels": [
                binding.as_dict() for binding in channel_resolver.bindings_for_voicebot(workspace_id, voicebot_id)
            ],
        }

    @app.post("/workspaces/{workspace_id}/voicebots/{voicebot_id}/channels")
    def create_voicebot_channel(
        workspace_id: str,
        voicebot_id: str,
        request: VoicebotChannelRequest,
    ) -> dict[str, Any]:
        require_workspace_access(workspace_id)
        try:
            binding = VoicebotChannelBinding(
                channel_id=request.channel_id,
                kind=request.kind,  # type: ignore[arg-type]
                workspace_id=workspace_id,
                voicebot_id=voicebot_id,
                external_id=request.external_id,
                enabled=request.enabled,
                metadata=request.metadata,
            )
            channel_resolver.register(binding)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"channel": binding.as_dict()}

    @app.get("/workspaces/{workspace_id}/voicebots/{voicebot_id}/channels/{channel_id}")
    def get_voicebot_channel(workspace_id: str, voicebot_id: str, channel_id: str) -> dict[str, Any]:
        require_workspace_access(workspace_id)
        binding = channel_resolver.get_channel(workspace_id, voicebot_id, channel_id)
        if binding is None:
            raise HTTPException(status_code=404, detail="Channel not found")
        return {"channel": binding.as_dict()}

    @app.patch("/workspaces/{workspace_id}/voicebots/{voicebot_id}/channels/{channel_id}")
    def patch_voicebot_channel(
        workspace_id: str,
        voicebot_id: str,
        channel_id: str,
        request: VoicebotChannelPatchRequest,
    ) -> dict[str, Any]:
        require_workspace_access(workspace_id)
        try:
            binding = channel_resolver.patch_channel(
                workspace_id,
                voicebot_id,
                channel_id,
                enabled=request.enabled,
                metadata=request.metadata,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="Channel not found") from None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"channel": binding.as_dict()}

    @app.delete("/workspaces/{workspace_id}/voicebots/{voicebot_id}/channels/{channel_id}")
    def delete_voicebot_channel(workspace_id: str, voicebot_id: str, channel_id: str) -> dict[str, Any]:
        require_workspace_access(workspace_id)
        binding = channel_resolver.unregister_voicebot_channel(workspace_id, voicebot_id, channel_id)
        if binding is None:
            raise HTTPException(status_code=404, detail="Channel not found")
        return {"channel": binding.as_dict(), "deleted": True}

    @app.post("/workspaces/{workspace_id}/voicebots/{voicebot_id}/validate")
    def validate_voicebot_runtime(workspace_id: str, voicebot_id: str) -> dict[str, Any]:
        require_workspace_access(workspace_id)
        issues: list[dict[str, Any]] = []
        voicebot = voicebot_store.get(workspace_id, voicebot_id)
        channels = channel_resolver.bindings_for_voicebot(workspace_id, voicebot_id)
        config = provider_config_store.get(workspace_id, voicebot_id)
        selection_plan = None

        if voicebot is None:
            issues.append({"area": "voicebot", "message": "voicebot record is missing"})
        elif not voicebot.enabled:
            issues.append({"area": "voicebot", "message": "voicebot is disabled"})

        if not channels:
            issues.append({"area": "channel", "message": "voicebot has no channel bindings"})
        elif not any(channel.enabled for channel in channels):
            issues.append({"area": "channel", "message": "voicebot has no enabled channel bindings"})

        if config is None:
            issues.append({"area": "provider", "message": "provider config is missing"})
        else:
            descriptors = {
                "stt": provider_catalog_descriptors("stt"),
                "tts": provider_catalog_descriptors("tts"),
                "agent": provider_catalog_descriptors("agent"),
            }
            issues.extend({"area": "provider", **validation_issue_to_dict(issue)} for issue in validate_provider_config(config, descriptors))
            if not any(issue["area"] == "provider" for issue in issues):
                selection_plan = selection_plan_to_dict(provider_selection_plan(config))

        return {
            "ok": len(issues) == 0,
            "workspace_id": workspace_id,
            "voicebot_id": voicebot_id,
            "channel_count": len(channels),
            "enabled_channel_count": len([channel for channel in channels if channel.enabled]),
            "selection_plan": selection_plan,
            "issues": issues,
        }

    @app.get("/workspaces/{workspace_id}/voicebots/{voicebot_id}/sessions")
    def list_voicebot_sessions(
        workspace_id: str,
        voicebot_id: str,
        active_only: bool = False,
    ) -> dict[str, Any]:
        require_workspace_access(workspace_id)
        sessions = voicebot_session_store.list(
            workspace_id=workspace_id,
            voicebot_id=voicebot_id,
            active_only=active_only,
        )
        return {
            "workspace_id": workspace_id,
            "voicebot_id": voicebot_id,
            "sessions": [session.as_dict() for session in sessions],
        }

    @app.get("/workspaces/{workspace_id}/voicebots/{voicebot_id}/sessions/{session_id}")
    def get_voicebot_session(workspace_id: str, voicebot_id: str, session_id: str) -> dict[str, Any]:
        require_workspace_access(workspace_id)
        session = voicebot_session_store.get(session_id, workspace_id=workspace_id)
        if session is None or session.voicebot_id != voicebot_id:
            raise HTTPException(status_code=404, detail="Session not found")
        return {"session": session.as_dict()}

    @app.get("/workspaces/{workspace_id}/voicebots/{voicebot_id}/sessions/{session_id}/timeline")
    def get_voicebot_session_timeline(
        workspace_id: str,
        voicebot_id: str,
        session_id: str,
        after: int = 0,
        limit: int = 200,
    ) -> dict[str, Any]:
        require_workspace_access(workspace_id)
        session = voicebot_session_store.get(session_id, workspace_id=workspace_id)
        if session is None or session.voicebot_id != voicebot_id:
            raise HTTPException(status_code=404, detail="Session not found")
        timeline = events.list_events(
            after=after,
            limit=validated_limit(limit),
            workspace_id=workspace_id,
            voicebot_id=voicebot_id,
            session_id=session_id,
        )
        return {
            "workspace_id": workspace_id,
            "voicebot_id": voicebot_id,
            "session_id": session_id,
            "events": [event_to_dict(event) for event in timeline],
        }

    @app.get("/workspaces/{workspace_id}/voicebots/{voicebot_id}/sessions/{session_id}/transcript")
    def get_voicebot_session_transcript(
        workspace_id: str,
        voicebot_id: str,
        session_id: str,
        after: int = 0,
        limit: int = 200,
    ) -> dict[str, Any]:
        require_workspace_access(workspace_id)
        session = voicebot_session_store.get(session_id, workspace_id=workspace_id)
        if session is None or session.voicebot_id != voicebot_id:
            raise HTTPException(status_code=404, detail="Session not found")
        transcript = transcripts.read(session_id, after=after, limit=validated_limit(limit))
        return {
            "workspace_id": workspace_id,
            "voicebot_id": voicebot_id,
            "session_id": session_id,
            "events": transcript,
        }

    @app.get("/workspaces/{workspace_id}/voicebots/{voicebot_id}/tasks")
    def list_voicebot_external_tasks(
        workspace_id: str,
        voicebot_id: str,
        session_id: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        require_workspace_access(workspace_id)
        if subagent_coordinator is None:
            raise HTTPException(status_code=503, detail="Subagent coordinator is not configured")
        tasks = [
            task
            for task in subagent_coordinator.store.list(workspace_id=workspace_id, session_id=session_id)
            if task.voicebot_id == voicebot_id and (status is None or task.status == status)
        ]
        return {
            "workspace_id": workspace_id,
            "voicebot_id": voicebot_id,
            "session_id": session_id,
            "status": status,
            "tasks": [subagent_task_to_dict(task) for task in tasks],
        }

    @app.get("/workspaces/{workspace_id}/voicebots/{voicebot_id}/providers")
    def get_voicebot_provider_config(workspace_id: str, voicebot_id: str) -> dict[str, Any]:
        require_workspace_access(workspace_id)
        config = provider_config_store.get(workspace_id, voicebot_id)
        if config is None:
            raise HTTPException(status_code=404, detail="Provider config not found")
        return {
            "config": provider_config_to_dict(config),
            "selection_plan": selection_plan_to_dict(provider_selection_plan(config)),
            "validation": [],
        }

    @app.get("/workspaces/{workspace_id}/voicebots/{voicebot_id}/transports")
    def get_voicebot_transport_catalog(workspace_id: str, voicebot_id: str) -> dict[str, Any]:
        require_workspace_access(workspace_id)
        return {
            "workspace_id": workspace_id,
            "voicebot_id": voicebot_id,
            **transport_catalog(),
        }

    @app.put("/workspaces/{workspace_id}/voicebots/{voicebot_id}/providers")
    def put_voicebot_provider_config(
        workspace_id: str,
        voicebot_id: str,
        request: VoicebotProviderConfigRequest,
    ) -> dict[str, Any]:
        require_workspace_access(workspace_id)
        try:
            config = VoicebotProviderConfig(
                workspace_id=workspace_id,
                voicebot_id=voicebot_id,
                stt=provider_choice_from_request("stt", request.stt, workspace_id),
                tts=provider_choice_from_request("tts", request.tts, workspace_id),
                agent=provider_choice_from_request("agent", request.agent, workspace_id),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        descriptors = {
            "stt": provider_catalog_descriptors("stt"),
            "tts": provider_catalog_descriptors("tts"),
            "agent": provider_catalog_descriptors("agent"),
        }
        issues = validate_provider_config(config, descriptors)
        if issues:
            return {
                "ok": False,
                "config": provider_config_to_dict(config),
                "validation": [validation_issue_to_dict(issue) for issue in issues],
            }
        saved = provider_config_store.save(config)
        return {
            "ok": True,
            "config": provider_config_to_dict(saved),
            "selection_plan": selection_plan_to_dict(provider_selection_plan(saved)),
            "validation": [],
        }

    @app.get("/workspaces/{workspace_id}/voicebots/{voicebot_id}/runtime-config")
    def get_voicebot_runtime_config(workspace_id: str, voicebot_id: str) -> dict[str, Any]:
        require_workspace_access(workspace_id)
        config = runtime_config_store.get(workspace_id, voicebot_id)
        if config is None:
            raise HTTPException(status_code=404, detail="Runtime config not found")
        return {"config": runtime_config_to_dict(config)}

    @app.put("/workspaces/{workspace_id}/voicebots/{voicebot_id}/runtime-config")
    async def put_voicebot_runtime_config(
        workspace_id: str,
        voicebot_id: str,
        request: VoicebotRuntimeConfigRequest,
    ) -> dict[str, Any]:
        require_workspace_access(workspace_id)
        try:
            providers = VoicebotProviderConfig(
                workspace_id=workspace_id,
                voicebot_id=voicebot_id,
                stt=provider_choice_from_request("stt", request.providers.stt, workspace_id),
                tts=provider_choice_from_request("tts", request.providers.tts, workspace_id),
                agent=provider_choice_from_request("agent", request.providers.agent, workspace_id),
            )
            config = VoicebotRuntimeConfig(
                workspace_id=workspace_id,
                voicebot_id=voicebot_id,
                config_version=1,
                providers=providers,
                prompts=VoicebotPromptConfig(
                    greeting=request.prompts.greeting,
                    system_prompt=request.prompts.system_prompt,
                    stt_prompt=request.prompts.stt_prompt,
                    language=request.prompts.language,
                ),
                realtime=VoicebotRealtimeConfig(**request.realtime.model_dump()),
                quotas=VoicebotQuotaConfig(
                    max_concurrent_sessions=request.quotas.max_concurrent_sessions,
                    max_provider_inflight=request.quotas.max_provider_inflight,
                    enabled_actions=tuple(request.quotas.enabled_actions),
                ),
                subagents=VoicebotSubagentConfig(**request.subagents.model_dump()),
                enabled=request.enabled,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        descriptors = {
            "stt": provider_catalog_descriptors("stt"),
            "tts": provider_catalog_descriptors("tts"),
            "agent": provider_catalog_descriptors("agent"),
        }
        issues = validate_provider_config(config.providers, descriptors)
        if issues:
            return {
                "ok": False,
                "config": runtime_config_to_dict(config),
                "validation": [validation_issue_to_dict(issue) for issue in issues],
            }
        saved = runtime_config_store.save(config)
        provider_config_store.save(saved.providers)
        event = events.append(
            "system",
            "runtime_config_updated",
            {
                "workspace_id": workspace_id,
                "voicebot_id": voicebot_id,
                "config_version": saved.config_version,
                "enabled": saved.enabled,
            },
        )
        await hub.broadcast(event)
        return {
            "ok": True,
            "config": runtime_config_to_dict(saved),
            "event": event_to_dict(event),
            "validation": [],
        }

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
                baseline_sessions=request.baseline_sessions,
                call_growth_per_minute=request.call_growth_per_minute,
                worker_warmup_seconds=request.worker_warmup_seconds,
                max_concurrent_sessions=request.max_concurrent_sessions,
                burst_sessions=request.burst_sessions,
                scale_to_zero_allowed=request.scale_to_zero_allowed,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return build_workload_plan(profile)

    @app.get("/scaling/signals")
    def scaling_signals(
        workspace_id: str | None = None,
        voicebot_id: str | None = None,
        format: str = "json",
    ):
        signals = autoscaling_signals(
            active_session_snapshots=registry.snapshots(),
            worker_registry=scaling_workers,
            worker_queue=scaling_queue,
            events=events.list_events(limit=1000),
            workspace_id=workspace_id,
            voicebot_id=voicebot_id,
        )
        if format == "prometheus":
            return PlainTextResponse(autoscaling_signals_prometheus(signals), media_type="text/plain; version=0.0.4")
        if format != "json":
            raise HTTPException(status_code=400, detail="format must be json or prometheus")
        return signals

    @app.post("/scaling/admission")
    def scaling_admission(request: ScalingAdmissionRequest) -> dict[str, Any]:
        try:
            policy = WarmCapacityPolicy(
                max_concurrent_sessions=request.max_concurrent_sessions,
                burst_sessions=request.burst_sessions,
                scale_to_zero_allowed=request.scale_to_zero_allowed,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        decision = admission_decision(
            active_session_snapshots=registry.snapshots(),
            workspace_id=request.workspace_id,
            voicebot_id=request.voicebot_id,
            policy=policy,
        )
        if not decision["allowed"]:
            events.append(
                request.workspace_id,
                "metrics",
                {
                    "name": "capacity_rejection",
                    "value": 1.0,
                    "workspace_id": request.workspace_id,
                    "voicebot_id": request.voicebot_id,
                    "reason": decision["reason"],
                },
            )
        return decision

    @app.post("/routing/admission")
    def routing_admission(request: IncomingSessionAdmissionRequest) -> dict[str, Any]:
        try:
            admission_request = IncomingSessionRequest(
                channel_kind=validated_channel_kind(request.channel_kind),
                external_id=request.external_id,
                session_id=request.session_id,
                owner=request.owner,
                transport=request.transport,
                call_id=request.call_id,
                acquire_lease=request.acquire_lease,
                lease_ttl_seconds=request.lease_ttl_seconds,
                max_concurrent_sessions=request.max_concurrent_sessions,
                burst_sessions=request.burst_sessions,
            )
            decision = evaluate_incoming_session(
                admission_request,
                channel_resolver=channel_resolver,
                voicebot_store=voicebot_store,
                provider_config_store=provider_config_store,
                runtime_config_store=runtime_config_store,
                workspace_access_policy=workspace_access_policy,
                session_lease_store=session_lease_store,
                active_session_snapshots=registry.snapshots(),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        event = events.append(
            decision.get("call_id") or decision.get("session_id") or request.external_id,
            "session_admission_decided",
            {
                key: value
                for key, value in decision.items()
                if key not in {"lease"}
            },
        )
        if not decision["allowed"]:
            events.append(
                decision.get("workspace_id") or request.external_id,
                "metrics",
                {
                    "name": "capacity_rejection",
                    "value": 1.0,
                    "reason": decision["reason"],
                    "workspace_id": decision.get("workspace_id"),
                    "voicebot_id": decision.get("voicebot_id"),
                    "transport": request.transport,
                },
            )
        return {"event_id": event.id, **decision}

    @app.post("/scaling/workers/heartbeat")
    def scaling_worker_heartbeat(request: WorkerHeartbeatRequest) -> dict[str, Any]:
        try:
            worker = scaling_workers.heartbeat(
                WorkerInstance(
                    worker_id=request.worker_id,
                    role=validated_worker_role(request.role),
                    queue=request.queue,
                    workspace_id=request.workspace_id,
                    voicebot_id=request.voicebot_id,
                    capacity=request.capacity,
                    status=validated_worker_status(request.status),
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"worker": worker.as_dict()}

    @app.get("/scaling/workers")
    def scaling_worker_list(
        role: str | None = None,
        workspace_id: str | None = None,
        voicebot_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            worker_role = validated_worker_role(role) if role else None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        workers = scaling_workers.active(role=worker_role, workspace_id=workspace_id, voicebot_id=voicebot_id)
        return {"workers": [worker.as_dict() for worker in workers]}

    @app.post("/scaling/workers/{worker_id}/drain")
    def scaling_worker_drain(worker_id: str) -> dict[str, Any]:
        try:
            worker = scaling_workers.mark_draining(worker_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="worker not found") from None
        return {"worker": worker.as_dict()}

    @app.delete("/scaling/workers/{worker_id}")
    def scaling_worker_remove(worker_id: str) -> dict[str, Any]:
        return {"removed": scaling_workers.remove(worker_id)}

    @app.get("/scaling/capacity")
    def scaling_capacity(workspace_id: str | None = None, voicebot_id: str | None = None) -> dict[str, Any]:
        return scaling_workers.capacity_summary(workspace_id=workspace_id, voicebot_id=voicebot_id)

    @app.get("/scaling/backpressure")
    def scaling_backpressure_snapshot() -> dict[str, Any]:
        return scaling_backpressure.snapshot()

    @app.get("/scaling/session-leases")
    def scaling_session_lease_snapshot(workspace_id: str | None = None, voicebot_id: str | None = None) -> dict[str, Any]:
        return {"leases": [lease.as_dict() for lease in session_lease_store.list(workspace_id=workspace_id, voicebot_id=voicebot_id)]}

    @app.post("/scaling/session-leases/acquire")
    def scaling_session_lease_acquire(request: SessionLeaseRequest) -> dict[str, Any]:
        try:
            lease = session_lease_store.acquire(
                request.workspace_id,
                request.voicebot_id,
                request.session_id,
                request.owner,
                request.ttl_seconds,
                call_id=request.call_id,
                transport=request.transport,
                metadata=request.metadata,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        if lease is not None:
            events.append(lease.call_id or lease.session_id, "session_lease_acquired", lease.as_dict())
        return {"acquired": lease is not None, "lease": lease.as_dict() if lease is not None else None}

    @app.post("/scaling/session-leases/renew")
    def scaling_session_lease_renew(request: SessionLeaseRequest) -> dict[str, Any]:
        try:
            lease = session_lease_store.renew(
                request.workspace_id,
                request.voicebot_id,
                request.session_id,
                request.owner,
                request.ttl_seconds,
                call_id=request.call_id,
                transport=request.transport,
                metadata=request.metadata,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        if lease is not None:
            events.append(lease.call_id or lease.session_id, "session_lease_renewed", lease.as_dict())
        return {"renewed": lease is not None, "lease": lease.as_dict() if lease is not None else None}

    @app.post("/scaling/session-leases/release")
    def scaling_session_lease_release(request: SessionLeaseReleaseRequest) -> dict[str, Any]:
        lease = session_lease_store.release(
            request.workspace_id,
            request.voicebot_id,
            request.session_id,
            owner=request.owner,
        )
        if lease is not None:
            events.append(lease.call_id or lease.session_id, "session_lease_released", lease.as_dict())
        return {"released": lease is not None, "lease": lease.as_dict() if lease is not None else None}

    @app.post("/scaling/session-leases/expire")
    def scaling_session_lease_expire() -> dict[str, Any]:
        expired = session_lease_store.expire()
        for lease in expired:
            events.append(lease.call_id or lease.session_id, "session_lease_expired", lease.as_dict())
        return {"expired": [lease.as_dict() for lease in expired]}

    @app.post("/scaling/session-leases/enforce")
    def scaling_session_lease_enforce(request: SessionLeaseEnforceRequest) -> dict[str, Any]:
        expired = session_lease_store.expire()
        for lease in expired:
            events.append(lease.call_id or lease.session_id, "session_lease_expired", lease.as_dict())
        interrupted = []
        recovered = []
        for snapshot in registry.snapshots():
            identity = session_identity_from_snapshot(snapshot)
            if identity is None:
                continue
            lease = session_lease_store.get(identity["workspace_id"], identity["voicebot_id"], identity["session_id"])
            if lease is not None and lease.owner == request.owner:
                continue
            loss_data = {
                **identity,
                "expected_owner": request.owner,
                "current_owner": lease.owner if lease is not None else None,
                "reason": "lease_owner_mismatch" if lease is not None else "lease_missing",
            }
            events.append(identity["call_id"], "session_lease_lost", loss_data)
            if request.recover_non_media_work:
                recovered_event = events.append(
                    identity["call_id"],
                    "session_recovered",
                    {**loss_data, "recovered_work": ["subagent_polling", "transcript_storage", "late_task_results"]},
                )
                recovered.append(event_to_dict(recovered_event))
            if request.stop_unleased_sessions:
                stopped = registry.stop(identity["call_id"])
                interrupted_event = events.append(identity["call_id"], "session_interrupted", {**loss_data, "stopped": stopped})
                interrupted.append(event_to_dict(interrupted_event))
        return {
            "expired": [lease.as_dict() for lease in expired],
            "recovered": recovered,
            "interrupted": interrupted,
        }

    @app.post("/scaling/backpressure/acquire")
    def scaling_backpressure_acquire(request: ScalingBackpressureRequest) -> dict[str, Any]:
        try:
            key = backpressure_key_from_request(request)
            acquired = scaling_backpressure.acquire(key)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"acquired": acquired, "key": key, **scaling_backpressure.snapshot()}

    @app.post("/scaling/backpressure/release")
    def scaling_backpressure_release(request: ScalingBackpressureRequest) -> dict[str, Any]:
        try:
            key = backpressure_key_from_request(request)
            scaling_backpressure.release(key)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"released": True, "key": key, **scaling_backpressure.snapshot()}

    @app.get("/scaling/queue")
    def scaling_queue_snapshot() -> dict[str, Any]:
        return scaling_queue.snapshot()

    @app.post("/scaling/queue/enqueue")
    def scaling_queue_enqueue(request: WorkerQueueEnqueueRequest) -> dict[str, Any]:
        try:
            envelope = scaling_queue.enqueue(
                WorkerQueueEnvelope(
                    item_id=request.item_id,
                    kind=request.kind,  # type: ignore[arg-type]
                    routing=RoutingKey(
                        workspace_id=request.routing.workspace_id,
                        voicebot_id=request.routing.voicebot_id,
                        session_id=request.routing.session_id,
                        provider=request.routing.provider,
                    ),
                    queue=request.queue,
                    payload=request.payload,
                    trace_id=request.trace_id,
                    created_at=request.created_at or datetime.now().astimezone().isoformat(),
                    attempt=request.attempt,
                    idempotency_key=request.idempotency_key,
                    max_attempts=request.max_attempts,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"item": envelope.as_dict()}

    @app.post("/scaling/queue/claim")
    def scaling_queue_claim(request: WorkerQueueClaimRequest) -> dict[str, Any]:
        try:
            claimed = scaling_queue.claim(
                request.queue,
                request.owner,
                limit=request.limit,
                ttl_seconds=request.ttl_seconds,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"items": [item.as_dict() for item in claimed]}

    @app.post("/scaling/queue/renew")
    def scaling_queue_renew(request: WorkerQueueItemRequest) -> dict[str, Any]:
        if request.owner is None:
            raise HTTPException(status_code=400, detail="owner is required")
        try:
            item = scaling_queue.renew(request.item_id, request.owner, ttl_seconds=request.ttl_seconds)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        if item is None:
            raise HTTPException(status_code=404, detail="work item claim not found")
        return {"item": item.as_dict(), "renewed": True}

    @app.post("/scaling/queue/ack")
    def scaling_queue_ack(request: WorkerQueueItemRequest) -> dict[str, Any]:
        item = scaling_queue.ack(request.item_id, owner=request.owner)
        if item is None:
            raise HTTPException(status_code=404, detail="work item claim not found")
        return {"item": item.as_dict(), "acked": True}

    @app.post("/scaling/queue/release")
    def scaling_queue_release(request: WorkerQueueItemRequest) -> dict[str, Any]:
        item = scaling_queue.release(request.item_id, owner=request.owner, error=request.error)
        if item is None:
            raise HTTPException(status_code=404, detail="work item claim not found")
        return {"item": item.as_dict(), "released": item.failed_at is None, "dead_lettered": item.failed_at is not None}

    @app.get("/scaling/queue/dead-letter")
    def scaling_queue_dead_letter() -> dict[str, Any]:
        return {"items": [item.as_dict() for item in scaling_queue.dead_lettered()]}

    def provider_choice_from_request(family: str, request, workspace_id: str) -> ProviderChoice:
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
            secret_ref=secret_ref,
            fallback_provider=request.fallback_provider,
            config=request.config,
        )

    def provider_catalog_descriptors(family: str):
        if family == "stt":
            return _stt_capabilities()
        if family == "tts":
            return _tts_capabilities()
        return _agent_capabilities()

    def backpressure_key_from_request(request: ScalingBackpressureRequest) -> str:
        routing = RoutingKey(
            workspace_id=request.workspace_id,
            voicebot_id=request.voicebot_id,
            session_id=request.session_id,
            provider=request.provider,
        )
        return routing.provider_key() if request.provider else routing.partition_key()

    def session_identity_from_snapshot(snapshot: dict[str, Any]) -> dict[str, str] | None:
        route = snapshot.get("route") if isinstance(snapshot.get("route"), dict) else {}
        workspace_id = non_empty_str(route.get("workspace_id") or snapshot.get("workspace_id"))
        voicebot_id = non_empty_str(route.get("voicebot_id") or snapshot.get("voicebot_id"))
        call_id = non_empty_str(snapshot.get("call_id"))
        if workspace_id is None or voicebot_id is None or call_id is None:
            return None
        session_id = non_empty_str(snapshot.get("session_id")) or call_id
        return {
            "workspace_id": workspace_id,
            "voicebot_id": voicebot_id,
            "session_id": session_id,
            "call_id": call_id,
            "transport": str(snapshot.get("transport") or ""),
        }

    def non_empty_str(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @app.get("/config")
    def config() -> dict[str, Any]:
        return {"settings": redacted_settings(runtime_settings)}

    @app.get("/webrtc/sessions")
    def list_webrtc_sessions() -> dict[str, Any]:
        if webrtc is None:
            raise HTTPException(status_code=503, detail="WebRTC transport is not configured")
        return {"sessions": webrtc.snapshots()}

    @app.get("/webrtc/media-plane")
    def get_webrtc_media_plane() -> dict[str, Any]:
        return webrtc_media_plane_payload()

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

    @app.get("/sip/media-plane")
    def get_sip_media_plane() -> dict[str, Any]:
        return sip_media_plane_payload()

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
        return {"events": event_catalog(), "integrity_issues": event_catalog_integrity_issues()}

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

    @app.get("/subagent/tasks/lifecycle")
    def subagent_task_lifecycle(workspace_id: str | None = None, session_id: str | None = None) -> dict[str, Any]:
        if subagent_lifecycle is None:
            raise HTTPException(status_code=503, detail="Subagent lifecycle runner is not configured")
        return {"lifecycle": subagent_lifecycle.snapshot(workspace_id=workspace_id, session_id=session_id)}

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
        source_event_id = _optional_int(data.get("source_event_id"))
        if source_event_id is not None and reason in {"colleague_result", "colleague_progress"}:
            source_event = events.get_event(source_event_id)
            elapsed = _seconds_between_timestamps(source_event.timestamp if source_event else "", requested.timestamp)
            if elapsed is not None:
                metric = events.append(
                    call_id,
                    "metrics",
                    {
                        "name": "colleague_result_to_agent_request_seconds",
                        "value": elapsed,
                        "source_event_id": source_event_id,
                        "event_id": requested.id,
                        "reason": reason,
                    },
                )
                await hub.broadcast(metric)

    async def notify_subagent_terminal_task(task: SubagentTask, terminal_event: VoicebotEvent | None = None) -> None:
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
                source_event_id=terminal_event.id if terminal_event else None,
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
                source_event_id=terminal_event.id if terminal_event else None,
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
                terminal_events_by_task_id = {
                    str(event.data.get("task_id")): event
                    for event in pending_broadcasts
                    if event.data.get("task_id")
                }
                for event in pending_broadcasts:
                    await hub.broadcast(event)
                for task in changed:
                    if task.is_terminal() and task.terminal_event_emitted_at:
                        await notify_subagent_terminal_task(task, terminal_events_by_task_id.get(task.task_id))
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


def validated_worker_role(value: str) -> WorkerRole:
    if value not in get_args(WorkerRole):
        raise ValueError(f"unsupported worker role: {value}")
    return value  # type: ignore[return-value]


def validated_worker_status(value: str):
    if value not in {"active", "draining"}:
        raise ValueError(f"unsupported worker status: {value}")
    return value


def validated_channel_kind(value: str) -> ChannelKind:
    if value not in get_args(ChannelKind):
        raise ValueError(f"unsupported channel kind: {value}")
    return value  # type: ignore[return-value]


def optional_int_arg(args: dict[str, Any], name: str, default: int) -> int:
    return optional_int_value(args.get(name, default), name, default)


def optional_int_value(value: Any, name: str, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{name} must be an integer") from None


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _seconds_between_timestamps(start: Any, end: Any) -> float | None:
    try:
        started = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        ended = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, (ended - started).total_seconds())


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
