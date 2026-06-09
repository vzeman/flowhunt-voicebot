from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import html
import logging
import threading
from time import perf_counter
from typing import Any, get_args

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .agent_tasks import AgentTaskTracker
from .api_agent_tasks import (
    AgentTasksApiContext,
    agent_task_status_payload,
    agent_task_summary_payload,
    create_agent_tasks_router,
)
from .api_agent_tools import AgentToolsApiContext, create_agent_tools_router
from .api_audience import apply_route_audiences
from .api_calls import CallsApiContext, call_state_payload, create_calls_router
from .api_context import ContextApiContext, create_context_router
from .api_contracts import ContractsApiContext, create_contracts_router
from .api_discovery import DiscoveryApiContext, create_discovery_router
from .api_observability import ObservabilityApiContext, create_observability_router
from .api_models import (
    AgentResponseRequest,
    CallControlRequest,
    PlaybackInterruptRequest,
    PublicVoicebotRoutePatchRequest,
    PublicVoicebotRouteRequest,
    VoicebotAdminPatchRequest,
    VoicebotAdminRequest,
    VoicebotChannelPatchRequest,
    VoicebotChannelRequest,
    WebRTCOfferRequest,
)
from .api_events import EventsApiContext, create_events_router, events_payload, metrics_payload
from .api_providers import ProvidersApiContext, create_providers_router
from .api_runtime import RuntimeApiContext, create_runtime_router, runtime_config_payload
from .api_scaling import ScalingApiContext, create_scaling_router
from .api_security import SecurityApiContext, create_security_router
from .api_sip import SipApiContext, create_sip_router
from .api_subagents import SubagentsApiContext, create_subagents_router
from .api_transcripts import (
    TranscriptsApiContext,
    call_transcript_payload,
    create_transcripts_router,
    list_transcripts_payload,
    transcript_stats_payload,
    transcript_summaries_payload,
)
from .api_voicebot_runtime_config import VoicebotRuntimeConfigApiContext, create_voicebot_runtime_config_router
from .api_voicebot_sessions import VoicebotSessionsApiContext, create_voicebot_sessions_router
from .api_webrtc import WebRTCApiContext, create_webrtc_router
from .asterisk_control import AsteriskAMI, ControlResult
from .calls import AgentResponse, CallRegistry
from .config import Settings
from .drain import DrainState
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
from .internal_auth import (
    internal_scope_for_request,
    parse_internal_api_keys,
    route_audience_for_request,
    route_requires_internal_auth,
    validate_internal_api_key,
)
from .language import detected_session_language, is_auto_language
from .multimodal import Modality, ModalityCapabilities, MultimodalContextStore
from .progress import ProgressCadenceMemory, normalize_progress_message
from .public_access import FixedWindowPublicRateLimiter, origin_allowed
from .provider_catalog import _agent_capabilities, _stt_capabilities, _tts_capabilities
from .provider_config import (
    ProviderConfigStore,
    provider_selection_plan,
    selection_plan_to_dict,
    validate_provider_config,
    validation_issue_to_dict,
)
from .scaling import (
    WorkerQueueStore,
    WorkerRegistry,
    WorkspaceBackpressure,
)
from .session_leases import SessionLeaseStore
from .security_contract import redact_sensitive_data
from .sip_trunks import SipTrunkStore
from .runtime_config import (
    VoicebotChatPromptConfig,
    VoicebotPromptConfig,
    VoicebotPromptConfigStore,
    VoicebotRuntimeConfigStore,
    VoicebotSubagentConfig,
    SubagentPromptConfig,
)
from .realtime_quality import metric_latency_budget_seconds
from .subagents import SubagentCoordinator, SubagentTask, SubagentTaskRequest, subagent_task_to_dict
from .task_lifecycle import PollingPolicy, SubagentTaskLifecycleRunner, TaskLifecycleEventType
from .tool_executor import AgentToolExecutor
from .transcripts import TranscriptStore
from .webrtc import WebRTCSessionManager
from .workspace_access import WorkspaceAccessPolicy, workspace_access_policy_from_settings
from .workspace_model import (
    ChannelResolver,
    PublicVoicebotRoute,
    PublicVoicebotRouteStore,
    VoicebotChannelBinding,
    VoicebotDefinition,
    VoicebotSessionStore,
    VoicebotStore,
    normalize_public_path,
)

_MODALITIES = set(get_args(Modality))
_API_MULTIMODAL_CAPABILITIES = ModalityCapabilities(
    input=frozenset(_MODALITIES),
    output=frozenset(_MODALITIES),
)
logger = logging.getLogger(__name__)


class _SafeFormatDict(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return ""


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
    prompt_configs: VoicebotPromptConfigStore | None = None,
    audio_artifacts=None,
    public_routes: PublicVoicebotRouteStore | None = None,
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
    public_route_store = public_routes or PublicVoicebotRouteStore()
    voicebot_session_store = voicebot_sessions or VoicebotSessionStore()
    session_lease_store = session_leases or SessionLeaseStore()
    runtime_config_store = runtime_configs or VoicebotRuntimeConfigStore()
    prompt_config_store = prompt_configs or VoicebotPromptConfigStore()
    internal_keys = parse_internal_api_keys(runtime_settings.internal_api_keys)
    public_rate_limiter = FixedWindowPublicRateLimiter(runtime_settings.public_session_rate_limit_per_minute)

    @app.middleware("http")
    async def access_log_middleware(request: Request, call_next):
        start = perf_counter()
        request_id = request.headers.get("x-request-id", "").strip() or f"req-{int(start * 1000000)}"
        response = None
        try:
            response = await call_next(request)
            return response
        finally:
            status_code = response.status_code if response is not None else 500
            try:
                log_api_access(request, status_code, (perf_counter() - start) * 1000, request_id)
            except Exception:
                logger.exception("api access log write failed")

    @app.middleware("http")
    async def internal_auth_middleware(request: Request, call_next):
        if not runtime_settings.internal_auth_enabled or not route_requires_internal_auth(request.method, request.url.path):
            return await call_next(request)
        scope = internal_scope_for_request(request.method, request.url.path)
        result = validate_internal_api_key(
            request.headers.get(runtime_settings.internal_auth_header),
            internal_keys,
            scope,
        )
        if not result.ok:
            events.append(
                "system",
                "internal_api_auth_denied",
                {
                    "method": request.method,
                    "path": request.url.path,
                    "reason": result.reason,
                    "scope": result.scope,
                    **({"key_id": result.key.key_id} if result.key is not None else {}),
                },
            )
            return JSONResponse(
                {"detail": result.reason, "scope": result.scope},
                status_code=result.status_code,
            )
        events.append(
            "system",
            "internal_api_auth_accepted",
            {
                "method": request.method,
                "path": request.url.path,
                "scope": result.scope,
                "key_id": result.key.key_id if result.key else "",
                "service": result.key.service if result.key else "",
            },
        )
        return await call_next(request)

    @app.middleware("http")
    async def dashboard_user_auth_middleware(request: Request, call_next):
        if not request.url.path.startswith("/dashboard"):
            return await call_next(request)
        auth = dashboard_user_auth(request)
        if not auth["ok"]:
            return JSONResponse({"detail": auth["reason"]}, status_code=auth["status_code"])
        request.state.dashboard_user = auth["user"]
        events.append(
            "system",
            "security_audit",
            {
                "action": "dashboard_request_authenticated",
                "path": request.url.path,
                "user_id": auth["user"]["user_id"],
                "workspace_ids": auth["user"]["workspace_ids"],
                "dev_login": auth["user"]["dev_login"],
            },
        )
        return await call_next(request)

    @app.middleware("http")
    async def public_widget_cors_middleware(request: Request, call_next):
        if request.url.path not in {"/.well-known/flowhunt-voicebot", "/webrtc/sessions", "/widget", "/widget.js"}:
            return await call_next(request)
        origin = request.headers.get("origin")
        route = None
        if origin:
            try:
                route = resolve_public_voicebot_route(request)
            except HTTPException:
                route = None
        if request.method == "OPTIONS":
            if route is not None and origin_allowed(origin, route.allowed_origins):
                return public_cors_response(origin)
            return Response(status_code=403)
        response = await call_next(request)
        if route is not None and origin_allowed(origin, route.allowed_origins):
            apply_public_cors_headers(response, origin)
        return response
    workspace_access_policy = workspace_policy or workspace_access_policy_from_settings(runtime_settings)
    delegated_progress_memory = ProgressCadenceMemory(runtime_settings.flowhunt_progress_update_seconds)
    subagent_terminal_events: list[VoicebotEvent] = []

    def require_workspace_access(workspace_id: str) -> None:
        try:
            workspace_access_policy.require_workspace(workspace_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from None

    def effective_prompt_config(workspace_id: str | None, voicebot_id: str | None) -> VoicebotPromptConfig:
        if workspace_id and voicebot_id:
            override = prompt_config_store.get(workspace_id, voicebot_id)
            if override is not None:
                return override
            runtime_config = runtime_config_store.get(workspace_id, voicebot_id)
            if runtime_config is not None:
                return runtime_config.prompts
        return VoicebotPromptConfig(
            greeting=runtime_settings.connect_greeting_prompt,
            system_prompt="",
            stt_prompt=runtime_settings.stt_prompt,
            language=runtime_settings.language or "auto",
            chat=VoicebotChatPromptConfig(
                mode=runtime_settings.chat_mode,
                system_prompt=runtime_settings.chat_system_prompt,
                response_prompt=runtime_settings.chat_response_prompt,
                rich_content_prompt=runtime_settings.chat_rich_content_prompt,
            ),
        )

    def prompt_config_from_request(data: Any) -> VoicebotPromptConfig:
        payload = data.model_dump()
        chat_payload = payload.pop("chat", {}) or {}
        return VoicebotPromptConfig(**payload, chat=VoicebotChatPromptConfig(**chat_payload))

    def default_subagent_config() -> VoicebotSubagentConfig:
        return VoicebotSubagentConfig(
            flowhunt_workspace_id=runtime_settings.flowhunt_workspace_id,
            flowhunt_flow_id=runtime_settings.flowhunt_flow_id,
            flowhunt_project_id=runtime_settings.flowhunt_project_id,
            complex_backend=runtime_settings.flowhunt_complex_backend,
        )

    def effective_subagent_config(workspace_id: str | None, voicebot_id: str | None) -> VoicebotSubagentConfig:
        if workspace_id and voicebot_id:
            runtime_config = runtime_config_store.get(workspace_id, voicebot_id)
            if runtime_config is not None:
                return runtime_config.subagents
        return default_subagent_config()

    def subagent_config_for_call(call_id: str) -> VoicebotSubagentConfig:
        snapshot = registry.snapshot(call_id)
        route = snapshot.get("route") if isinstance(snapshot, dict) else {}
        if not isinstance(route, dict):
            route = {}
        return effective_subagent_config(non_empty_str(route.get("workspace_id")), non_empty_str(route.get("voicebot_id")))

    def subagent_config_from_request(data: Any) -> VoicebotSubagentConfig:
        payload = data.model_dump()
        prompt_payload = payload.pop("prompts", {}) or {}
        prompts = {
            provider: SubagentPromptConfig(**prompt)
            for provider, prompt in prompt_payload.items()
            if isinstance(prompt, dict)
        }
        return VoicebotSubagentConfig(**payload, prompts=prompts)

    def explicit_subagent_prompt_for_call(call_id: str, provider: str) -> tuple[SubagentPromptConfig, bool]:
        config = subagent_config_for_call(call_id)
        return config.prompt_for(provider), provider in config.prompts

    def prompts_payload(workspace_id: str, voicebot_id: str) -> dict[str, Any]:
        prompts = effective_prompt_config(workspace_id, voicebot_id)
        source = "default"
        if prompt_config_store.get(workspace_id, voicebot_id) is not None:
            source = "prompt_override"
        elif runtime_config_store.get(workspace_id, voicebot_id) is not None:
            source = "runtime_config"
        return {
            "workspace_id": workspace_id,
            "voicebot_id": voicebot_id,
            "source": source,
            "prompts": prompts.as_dict(),
        }

    def prompt_config_for_call(call_id: str) -> VoicebotPromptConfig:
        snapshot = registry.snapshot(call_id)
        route = snapshot.get("route") if isinstance(snapshot, dict) else {}
        if not isinstance(route, dict):
            route = {}
        return effective_prompt_config(non_empty_str(route.get("workspace_id")), non_empty_str(route.get("voicebot_id")))

    def prompt_context_for_pending(pending: list[VoicebotEvent]) -> dict[str, Any]:
        by_call_id = {}
        session_languages = {}
        for event in pending:
            prompt_config = prompt_config_for_call(event.call_id).as_dict()
            detected_language = detected_session_language(events.list_events(call_id=event.call_id, limit=1000))
            if detected_language:
                session_languages[event.call_id] = detected_language
                if is_auto_language(str(prompt_config.get("language") or "")):
                    prompt_config = {
                        **prompt_config,
                        "language": detected_language["language"],
                        "language_source": "session_detected",
                        "detected_language": detected_language,
                    }
            by_call_id[event.call_id] = prompt_config
        context: dict[str, Any] = {
            "prompt_configs_by_call_id": by_call_id,
            "session_languages_by_call_id": session_languages,
        }
        if len(by_call_id) == 1:
            context["voicebot_prompts"] = next(iter(by_call_id.values()))
            if session_languages:
                context["session_language"] = next(iter(session_languages.values()))
        return context

    def agent_task_event_to_dict(event: VoicebotEvent, context: dict[str, Any]) -> dict[str, Any]:
        payload = event_to_dict(event)
        prompt_config = (context.get("prompt_configs_by_call_id") or {}).get(event.call_id)
        if isinstance(prompt_config, dict):
            payload["data"] = {
                **payload.get("data", {}),
                "prompt_config": prompt_config,
            }
        session_language = (context.get("session_languages_by_call_id") or {}).get(event.call_id)
        if isinstance(session_language, dict):
            payload["data"] = {
                **payload.get("data", {}),
                "session_language": session_language,
            }
        return payload

    def append_security_audit(
        *,
        workspace_id: str | None,
        action: str,
        actor: str = "runtime",
        voicebot_id: str | None = None,
        session_id: str | None = None,
        call_id: str | None = None,
        resource_type: str = "",
        resource_id: str | None = None,
        outcome: str = "requested",
        metadata: dict[str, Any] | None = None,
    ) -> VoicebotEvent:
        payload = redact_sensitive_data(
            {
                "workspace_id": workspace_id,
                "voicebot_id": voicebot_id,
                "session_id": session_id,
                "action": action,
                "actor": actor,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "outcome": outcome,
                "metadata": metadata or {},
            }
        )
        return events.append(call_id or session_id or "security", "security_audit", payload)

    def log_api_access(request: Request, status_code: int, latency_ms: float, request_id: str) -> None:
        path = request.url.path
        if path.startswith("/health/liveness"):
            return
        audience = route_audience_for_request(request.method, path)
        route_data: dict[str, Any] = {}
        if audience == "public":
            try:
                route = resolve_public_voicebot_route(request)
            except HTTPException:
                route = None
            if route is not None:
                route_data = route.event_data()
        user = getattr(request.state, "dashboard_user", None)
        payload = redact_sensitive_data(
            {
                "request_id": request_id,
                "audience": audience,
                "method": request.method,
                "path": path,
                "status_code": status_code,
                "latency_ms": round(latency_ms, 3),
                "origin": request.headers.get("origin", ""),
                "user_agent": request.headers.get("user-agent", "")[:160],
                "source_ip": "" if runtime_settings.pii_safe_logging_enabled else (request.client.host if request.client else ""),
                "source_ip_recorded": not runtime_settings.pii_safe_logging_enabled,
                "dashboard_user_id": (user or {}).get("user_id", ""),
                **route_data,
            }
        )
        events.append("access", "api_access_logged", payload)

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

    runtime_api_context = RuntimeApiContext(
        events=events,
        registry=registry,
        tracker=tracker,
        transcripts=transcripts,
        asterisk=asterisk,
        runtime_settings=runtime_settings,
        workspace_access_policy=workspace_access_policy,
        session_lease_store=session_lease_store,
        scaling_workers=scaling_workers,
        voicebot_session_store=voicebot_session_store,
        provider_config_store=provider_config_store,
        scaling_queue=scaling_queue,
        subagent_coordinator=subagent_coordinator,
        drain_state=drain_state,
    )
    app.include_router(create_runtime_router(runtime_api_context))
    events_api_context = EventsApiContext(
        events=events,
        transcripts=transcripts,
        durable_call_events=durable_call_events,
        validated_limit=validated_limit,
    )
    app.include_router(create_events_router(events_api_context))
    app.include_router(
        create_observability_router(
            ObservabilityApiContext(
                events=events,
                transcripts=transcripts,
                durable_call_events=durable_call_events,
                validated_limit=validated_limit,
            )
        )
    )
    app.include_router(
        create_security_router(
            SecurityApiContext(
                runtime_settings=runtime_settings,
                workspace_access_policy=workspace_access_policy,
                require_workspace_access=require_workspace_access,
                append_security_audit=append_security_audit,
                broadcast=hub.broadcast,
            )
        )
    )
    app.include_router(create_contracts_router(ContractsApiContext(runtime_settings=runtime_settings)))
    app.include_router(create_context_router(ContextApiContext(events=events, broadcast=hub.broadcast)))
    app.include_router(create_discovery_router(DiscoveryApiContext(app=app)))
    agent_tasks_api_context = AgentTasksApiContext(
        events=events,
        registry=registry,
        tracker=tracker,
        validated_limit=validated_limit,
        prompt_context_for_pending=prompt_context_for_pending,
        agent_task_event_to_dict=agent_task_event_to_dict,
    )
    app.include_router(create_agent_tasks_router(agent_tasks_api_context))
    app.include_router(
        create_providers_router(
            ProvidersApiContext(
                provider_config_store=provider_config_store,
                require_workspace_access=require_workspace_access,
                append_security_audit=append_security_audit,
            )
        )
    )
    calls_api_context = CallsApiContext(
        registry=registry,
        multimodal_store=multimodal_store,
        events=events,
        broadcast=hub.broadcast,
        multimodal_capabilities=_API_MULTIMODAL_CAPABILITIES,
        audio_artifacts=audio_artifacts,
    )
    app.include_router(create_calls_router(calls_api_context))
    transcripts_api_context = TranscriptsApiContext(
        transcripts=transcripts,
        optional_int_value=optional_int_value,
        validated_limit=validated_limit,
    )
    app.include_router(create_transcripts_router(transcripts_api_context))
    app.include_router(
        create_voicebot_sessions_router(
            VoicebotSessionsApiContext(
                voicebot_session_store=voicebot_session_store,
                events=events,
                transcripts=transcripts,
                subagent_coordinator=subagent_coordinator,
                require_workspace_access=require_workspace_access,
                durable_call_events=durable_call_events,
                validated_limit=validated_limit,
                append_security_audit=append_security_audit,
                broadcast=hub.broadcast,
            )
        )
    )
    app.include_router(
        create_voicebot_runtime_config_router(
            VoicebotRuntimeConfigApiContext(
                runtime_config_store=runtime_config_store,
                prompt_config_store=prompt_config_store,
                provider_config_store=provider_config_store,
                voicebot_store=voicebot_store,
                events=events,
                require_workspace_access=require_workspace_access,
                prompt_config_from_request=prompt_config_from_request,
                effective_prompt_config=effective_prompt_config,
                subagent_config_from_request=subagent_config_from_request,
                append_security_audit=append_security_audit,
                broadcast=hub.broadcast,
            )
        )
    )
    app.include_router(
        create_sip_router(
            SipApiContext(
                sip_trunks=sip_trunks,
                asterisk=asterisk,
                append_security_audit=append_security_audit,
            )
        )
    )
    app.include_router(
        create_scaling_router(
            ScalingApiContext(
                events=events,
                registry=registry,
                worker_registry=scaling_workers,
                worker_queue=scaling_queue,
                backpressure=scaling_backpressure,
                session_lease_store=session_lease_store,
                channel_resolver=channel_resolver,
                voicebot_store=voicebot_store,
                provider_config_store=provider_config_store,
                runtime_config_store=runtime_config_store,
                workspace_access_policy=workspace_access_policy,
            )
        )
    )

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

    @app.get("/workspaces/{workspace_id}/voicebots/{voicebot_id}/public-routes")
    def list_public_voicebot_routes(workspace_id: str, voicebot_id: str) -> dict[str, Any]:
        require_workspace_access(workspace_id)
        return {
            "workspace_id": workspace_id,
            "voicebot_id": voicebot_id,
            "routes": [route.as_dict() for route in public_route_store.list(workspace_id, voicebot_id)],
        }

    @app.post("/workspaces/{workspace_id}/voicebots/{voicebot_id}/public-routes")
    def create_public_voicebot_route(
        workspace_id: str,
        voicebot_id: str,
        request: PublicVoicebotRouteRequest,
    ) -> dict[str, Any]:
        require_workspace_access(workspace_id)
        if channel_resolver.get_channel(workspace_id, voicebot_id, request.channel_id) is None:
            raise HTTPException(status_code=404, detail="Channel not found")
        try:
            route = public_route_store.save(
                PublicVoicebotRoute(
                    route_id=request.route_id,
                    workspace_id=workspace_id,
                    voicebot_id=voicebot_id,
                    channel_id=request.channel_id,
                    host=request.host,
                    path_prefix=request.path_prefix,
                    status=request.status,  # type: ignore[arg-type]
                    tls_mode=request.tls_mode,  # type: ignore[arg-type]
                    allowed_origins=tuple(request.allowed_origins),
                    metadata=request.metadata,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"route": route.as_dict()}

    @app.patch("/workspaces/{workspace_id}/voicebots/{voicebot_id}/public-routes/{route_id}")
    def patch_public_voicebot_route(
        workspace_id: str,
        voicebot_id: str,
        route_id: str,
        request: PublicVoicebotRoutePatchRequest,
    ) -> dict[str, Any]:
        require_workspace_access(workspace_id)
        existing = public_route_store.get(route_id, workspace_id)
        if existing is None or existing.voicebot_id != voicebot_id:
            raise HTTPException(status_code=404, detail="Public route not found")
        channel_id = request.channel_id or existing.channel_id
        if channel_resolver.get_channel(workspace_id, voicebot_id, channel_id) is None:
            raise HTTPException(status_code=404, detail="Channel not found")
        try:
            route = public_route_store.save(
                PublicVoicebotRoute(
                    route_id=existing.route_id,
                    workspace_id=workspace_id,
                    voicebot_id=voicebot_id,
                    channel_id=channel_id,
                    host=request.host or existing.host,
                    path_prefix=request.path_prefix or existing.path_prefix,
                    status=(request.status or existing.status),  # type: ignore[arg-type]
                    tls_mode=(request.tls_mode or existing.tls_mode),  # type: ignore[arg-type]
                    allowed_origins=tuple(
                        existing.allowed_origins if request.allowed_origins is None else request.allowed_origins
                    ),
                    metadata=existing.metadata if request.metadata is None else request.metadata,
                    created_at=existing.created_at,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"route": route.as_dict()}

    @app.delete("/workspaces/{workspace_id}/voicebots/{voicebot_id}/public-routes/{route_id}")
    def delete_public_voicebot_route(workspace_id: str, voicebot_id: str, route_id: str) -> dict[str, Any]:
        require_workspace_access(workspace_id)
        route = public_route_store.get(route_id, workspace_id)
        if route is None or route.voicebot_id != voicebot_id:
            raise HTTPException(status_code=404, detail="Public route not found")
        deleted = public_route_store.delete(route_id, workspace_id)
        assert deleted is not None
        return {"route": deleted.as_dict(), "deleted": True}

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

    def provider_catalog_descriptors(family: str):
        if family == "stt":
            return _stt_capabilities()
        if family == "tts":
            return _tts_capabilities()
        return _agent_capabilities()

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

    @app.get("/dashboard")
    def dashboard() -> HTMLResponse:
        return HTMLResponse(
            DASHBOARD_PAGE.replace("__WEBRTC_CONSOLE_SRCDOC__", html.escape(WEBRTC_TEST_PAGE, quote=True)),
            headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"},
        )

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse(url="/dashboard")

    @app.get("/dashboard/state")
    def dashboard_state(request: Request, workspace_id: str | None = None) -> dict[str, Any]:
        dashboard_user = getattr(request.state, "dashboard_user", None)
        workspace_ids = dashboard_visible_workspace_ids(dashboard_user)
        selected_workspace = workspace_id or (workspace_ids[0] if workspace_ids else "")
        if selected_workspace and selected_workspace not in workspace_ids:
            if runtime_settings.dashboard_auth_enabled:
                raise HTTPException(status_code=403, detail="dashboard_workspace_access_denied")
            selected_workspace = workspace_ids[0] if workspace_ids else ""
        voicebot_rows = []
        if selected_workspace:
            active_counts = active_session_counts_by_voicebot()
            for voicebot in voicebot_store.list(selected_workspace):
                channels_for_voicebot = channel_resolver.bindings_for_voicebot(selected_workspace, voicebot.voicebot_id)
                routes_for_voicebot = public_route_store.list(selected_workspace, voicebot.voicebot_id)
                voicebot_rows.append(
                    {
                        **voicebot.as_dict(),
                        "channels": [binding.as_dict() for binding in channels_for_voicebot],
                        "public_routes": [route.as_dict() for route in routes_for_voicebot],
                        "active_sessions": active_counts.get((selected_workspace, voicebot.voicebot_id), 0),
                    }
                )
        return {
            "dashboard": {
                "access": "internal",
                "auth": "dashboard_user_login" if runtime_settings.dashboard_auth_enabled else "local_internal",
                "user": dashboard_user,
                "webrtc_console": "embedded",
            },
            "workspaces": workspace_ids,
            "workspace_rows": dashboard_workspace_rows(workspace_ids),
            "selected_workspace_id": selected_workspace,
            "voicebots": voicebot_rows,
            "active_sessions": dashboard_session_rows(active_only=True),
            "session_history": dashboard_session_rows(include_active_snapshots=True),
            "recent_events": [event_to_dict(event) for event in events.list_events(limit=80, workspace_id=selected_workspace or None)],
        }

    @app.get("/.well-known/flowhunt-voicebot")
    def public_voicebot_bootstrap(request: Request) -> dict[str, Any]:
        route = resolve_public_voicebot_route(request)
        if route is None:
            events.append(
                "system",
                "session_admission_decided",
                {
                    "transport": "webrtc",
                    "decision": "reject",
                    "reason": "public_route_not_found",
                    "host": request.headers.get("x-forwarded-host") or request.headers.get("host") or "",
                },
            )
            raise HTTPException(status_code=404, detail="Public voicebot route not found")
        voicebot = voicebot_store.get(route.workspace_id, route.voicebot_id)
        widget_config = caller_safe_widget_config(route, voicebot.display_name if voicebot else "")
        return {
            "route_id": route.route_id,
            "workspace_id": route.workspace_id,
            "voicebot_id": route.voicebot_id,
            "channel_id": route.channel_id,
            "display_name": voicebot.display_name if voicebot else "",
            "transport": "webrtc",
            "session_endpoint": "/webrtc/sessions",
            "widget_script": "/widget.js",
            "widget_page": "/widget",
            "widget": widget_config,
            "ice_servers": list(runtime_settings.webrtc_stun_urls),
            "modalities": {"input": ["audio"], "output": ["audio"]},
            "limits": {
                "sdp_max_bytes": runtime_settings.public_sdp_max_bytes,
                "rate_limit_per_minute": runtime_settings.public_session_rate_limit_per_minute,
                "max_concurrent_sessions": runtime_settings.public_voicebot_max_concurrent_sessions,
            },
        }

    @app.get("/widget.js")
    def public_widget_script() -> Response:
        return Response(
            content=VOICEBOT_WIDGET_JS,
            media_type="application/javascript; charset=utf-8",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    @app.get("/widget")
    def public_widget_page() -> HTMLResponse:
        return HTMLResponse(WIDGET_PAGE)

    def resolve_public_voicebot_route(request: Request) -> PublicVoicebotRoute | None:
        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
        path = (
            request.headers.get("x-forwarded-prefix")
            or request.headers.get("x-original-uri")
            or request.headers.get("x-forwarded-uri")
            or request.url.path
        )
        route = public_route_store.resolve(host, normalize_public_path(path))
        if route is None:
            return None
        voicebot = voicebot_store.get(route.workspace_id, route.voicebot_id)
        if voicebot is None:
            raise HTTPException(status_code=404, detail="Public route target voicebot not found")
        if not voicebot.enabled:
            raise HTTPException(status_code=403, detail="Voicebot is disabled")
        channel = channel_resolver.get_channel(route.workspace_id, route.voicebot_id, route.channel_id)
        if channel is None or not channel.enabled:
            raise HTTPException(status_code=403, detail="Public route channel is disabled")
        return route

    def caller_safe_widget_config(route: PublicVoicebotRoute, display_name: str) -> dict[str, Any]:
        metadata = route.metadata if isinstance(route.metadata, dict) else {}
        theme = metadata.get("theme") if isinstance(metadata.get("theme"), dict) else {}
        primary_color = str(theme.get("primary_color") or metadata.get("primary_color") or "#0969da")[:32]
        placement = str(theme.get("placement") or metadata.get("placement") or "bottom-right")[:32]
        launcher_label = str(metadata.get("launcher_label") or display_name or "Start voice call")[:80]
        return {
            "enabled": route.status == "active",
            "display_name": display_name,
            "launcher_label": launcher_label,
            "welcome_label": str(metadata.get("welcome_label") or "Voice call")[:80],
            "locale": str(metadata.get("locale") or "")[:32],
            "theme": {
                "primary_color": primary_color,
                "placement": placement,
            },
            "show_captions": bool(metadata.get("show_captions", False)),
            "visitor_metadata_max_bytes": 2048,
            "recording_visible_to_visitor": bool(metadata.get("recording_visible_to_visitor", False)),
        }

    def sanitized_public_visitor_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
        reserved = {
            "workspace_id",
            "voicebot_id",
            "channel_id",
            "public_route_id",
            "public_route_host",
            "public_route_path_prefix",
        }
        visitor = {str(key)[:64]: value for key, value in metadata.items() if key not in reserved}
        encoded = json_safe_size(visitor)
        if encoded > 2048:
            raise HTTPException(status_code=413, detail="Visitor metadata is too large")
        return visitor

    def json_safe_size(payload: dict[str, Any]) -> int:
        import json

        try:
            return len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Visitor metadata must be JSON serializable")

    def public_cors_response(origin: str | None) -> Response:
        response = Response(status_code=204)
        apply_public_cors_headers(response, origin)
        response.headers["Access-Control-Allow-Methods"] = "GET,POST,DELETE,OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "content-type"
        response.headers["Access-Control-Max-Age"] = "600"
        return response

    def apply_public_cors_headers(response: Response, origin: str | None) -> None:
        if not origin:
            return
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"

    def enforce_public_session_admission(
        route: PublicVoicebotRoute,
        request: Request,
        offer: WebRTCOfferRequest,
    ) -> None:
        if len(offer.sdp.encode("utf-8")) > runtime_settings.public_sdp_max_bytes:
            emit_public_admission(route, "reject", "sdp_too_large")
            raise HTTPException(status_code=413, detail="SDP offer is too large")
        if not origin_allowed(request.headers.get("origin"), route.allowed_origins):
            emit_public_admission(route, "reject", "origin_not_allowed")
            raise HTTPException(status_code=403, detail="Origin is not allowed for this voicebot route")
        active_count = active_public_voicebot_session_count(route)
        if active_count >= runtime_settings.public_voicebot_max_concurrent_sessions:
            emit_public_admission(route, "reject", "voicebot_session_capacity_full", {"active_sessions": active_count})
            raise HTTPException(status_code=429, detail="Voicebot session capacity is full")
        decision = public_rate_limiter.check_and_increment(route.route_id)
        if not decision.allowed:
            emit_public_admission(route, "reject", decision.reason, decision.to_dict())
            raise HTTPException(
                status_code=429,
                detail=decision.reason,
                headers={"Retry-After": str(decision.retry_after_seconds or 60)},
            )
        emit_public_admission(route, "accept", "accepted", {"active_sessions": active_count})

    def active_public_voicebot_session_count(route: PublicVoicebotRoute) -> int:
        if webrtc is None:
            return 0
        count = 0
        for snapshot in webrtc.snapshots():
            metadata = snapshot.get("metadata") if isinstance(snapshot.get("metadata"), dict) else {}
            route_data = snapshot.get("route") if isinstance(snapshot.get("route"), dict) else {}
            workspace_id = metadata.get("workspace_id") or route_data.get("workspace_id")
            voicebot_id = metadata.get("voicebot_id") or route_data.get("voicebot_id")
            if workspace_id == route.workspace_id and voicebot_id == route.voicebot_id:
                count += 1
        return count

    def active_session_counts_by_voicebot() -> dict[tuple[str, str], int]:
        counts: dict[tuple[str, str], int] = {}
        if webrtc is None:
            return counts
        for snapshot in webrtc.snapshots():
            metadata = snapshot.get("metadata") if isinstance(snapshot.get("metadata"), dict) else {}
            route_data = snapshot.get("route") if isinstance(snapshot.get("route"), dict) else {}
            workspace_id = metadata.get("workspace_id") or route_data.get("workspace_id")
            voicebot_id = metadata.get("voicebot_id") or route_data.get("voicebot_id")
            if workspace_id and voicebot_id:
                key = (str(workspace_id), str(voicebot_id))
                counts[key] = counts.get(key, 0) + 1
        return counts

    def dashboard_workspace_rows(workspace_ids: list[str]) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for workspace_id in workspace_ids:
            display_names = [
                voicebot.display_name
                for voicebot in voicebot_store.list(workspace_id)
                if voicebot.display_name
            ]
            rows.append(
                {
                    "workspace_id": workspace_id,
                    "name": workspace_id if not display_names else workspace_id,
                }
            )
        return rows

    def dashboard_session_rows(
        active_only: bool = False,
        ended_only: bool = False,
        include_active_snapshots: bool = False,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for session in voicebot_session_store.list(active_only=active_only):
            if ended_only and session.status != "ended":
                continue
            rows.append(session.as_dict())
        if (active_only or include_active_snapshots) and webrtc is not None:
            known_session_ids = {str(row.get("session_id")) for row in rows}
            for snapshot in webrtc.snapshots():
                session_id = str(snapshot.get("session_id") or "")
                if not session_id or session_id in known_session_ids:
                    continue
                metadata = snapshot.get("metadata") if isinstance(snapshot.get("metadata"), dict) else {}
                route = snapshot.get("route") if isinstance(snapshot.get("route"), dict) else {}
                workspace_id = str(metadata.get("workspace_id") or route.get("workspace_id") or "")
                voicebot_id = str(metadata.get("voicebot_id") or route.get("voicebot_id") or "")
                if not workspace_id or not voicebot_id:
                    continue
                rows.append(
                    {
                        "session_id": session_id,
                        "workspace_id": workspace_id,
                        "voicebot_id": voicebot_id,
                        "channel_id": metadata.get("channel_id") or route.get("channel_id"),
                        "external_session_id": snapshot.get("call_id"),
                        "status": "active",
                        "started_at": metadata.get("started_at") or "",
                        "ended_at": None,
                        "metadata": {"transport": snapshot.get("transport"), **metadata},
                    }
                )
        return sorted(rows, key=lambda item: str(item.get("started_at") or item.get("session_id") or ""), reverse=True)

    def dashboard_user_auth(request: Request) -> dict[str, Any]:
        if not runtime_settings.dashboard_auth_enabled:
            return {
                "ok": True,
                "user": {
                    "user_id": "local-dashboard",
                    "workspace_ids": list(voicebot_store.workspace_ids()),
                    "dev_login": False,
                },
            }
        if dashboard_dev_login_allowed(request):
            return {
                "ok": True,
                "user": {
                    "user_id": "dev-dashboard-user",
                    "workspace_ids": list(voicebot_store.workspace_ids()),
                    "dev_login": True,
                },
            }
        user_id = request.headers.get(runtime_settings.dashboard_user_id_header, "").strip()
        if not user_id:
            return {"ok": False, "status_code": 401, "reason": "dashboard_login_required"}
        workspace_ids = [
            item.strip()
            for item in request.headers.get(runtime_settings.dashboard_workspace_ids_header, "").split(",")
            if item.strip()
        ]
        return {
            "ok": True,
            "user": {
                "user_id": user_id,
                "workspace_ids": sorted(set(workspace_ids)),
                "dev_login": False,
            },
        }

    def dashboard_dev_login_allowed(request: Request) -> bool:
        if not runtime_settings.dashboard_dev_login_enabled:
            return False
        if runtime_settings.deployment_mode not in {"local", "development", "dev", "test"}:
            return False
        return request.headers.get("X-FlowHunt-Dev-Login", "").lower() in {"1", "true", "yes"}

    def dashboard_visible_workspace_ids(dashboard_user: dict[str, Any] | None) -> list[str]:
        all_workspaces = list(voicebot_store.workspace_ids())
        if not runtime_settings.dashboard_auth_enabled or not dashboard_user:
            return all_workspaces
        allowed = set(dashboard_user.get("workspace_ids") or [])
        if dashboard_user.get("dev_login"):
            return all_workspaces
        return [workspace_id for workspace_id in all_workspaces if workspace_id in allowed]

    def emit_public_admission(
        route: PublicVoicebotRoute,
        decision: str,
        reason: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        events.append(
            "system",
            "session_admission_decided",
            {
                "transport": "webrtc",
                "decision": decision,
                "reason": reason,
                **route.event_data(),
                **(extra or {}),
            },
        )

    app.include_router(
        create_webrtc_router(
            WebRTCApiContext(
                webrtc=webrtc,
                require_workspace_access=require_workspace_access,
                resolve_public_voicebot_route=resolve_public_voicebot_route,
                sanitized_public_visitor_metadata=sanitized_public_visitor_metadata,
                enforce_public_session_admission=enforce_public_session_admission,
                effective_prompt_config=effective_prompt_config,
                non_empty_str=non_empty_str,
            )
        )
    )

    @app.post("/calls/{call_id}/responses")
    async def submit_response(call_id: str, request: AgentResponseRequest) -> dict[str, Any]:
        session = registry.get(call_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"Active call not found: {call_id}")
        if request.finalize_only:
            tracker.mark_responded(request.response_to_event_id)
            event = events.append(
                call_id,
                "agent_response_received",
                {
                    "text": "",
                    "response_to_event_id": request.response_to_event_id,
                    "response_kind": request.response_kind or "stream_finalized",
                    "stream_finalized": True,
                },
            )
            await hub.broadcast(event)
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
        if not request.partial:
            tracker.mark_responded(request.response_to_event_id)
        await hub.broadcast(event)
        return {"event": event_to_dict(event), "ok": True}

    @app.post("/calls/{call_id}/control")
    async def call_control(call_id: str, request: CallControlRequest) -> dict[str, Any]:
        requested = events.append(call_id, "call_control_requested", request.model_dump())
        active_session = registry.get(call_id)
        active_snapshot = active_session.snapshot() if active_session is not None else None
        route = active_snapshot.get("route") if isinstance(active_snapshot, dict) else {}
        if not isinstance(route, dict):
            route = {}
        audit_session_id = None
        if isinstance(active_snapshot, dict):
            audit_session_id = route.get("session_id") or active_snapshot.get("session_id")
        audit_event = append_security_audit(
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
        await hub.broadcast(audit_event)
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

    @app.websocket("/ws/events")
    async def websocket_events(websocket: WebSocket) -> None:
        if runtime_settings.internal_auth_enabled:
            result = validate_internal_api_key(
                websocket.headers.get(runtime_settings.internal_auth_header),
                internal_keys,
                "diagnostics:read",
            )
            if not result.ok:
                events.append(
                    "system",
                    "internal_api_auth_denied",
                    {
                        "method": "WEBSOCKET",
                        "path": "/ws/events",
                        "reason": result.reason,
                        "scope": result.scope,
                        **({"key_id": result.key.key_id} if result.key is not None else {}),
                    },
                )
                await websocket.close(code=1008, reason=result.reason)
                return
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
            response_kind=args.get("response_kind"),
            chat=args.get("chat") if isinstance(args.get("chat"), dict) else None,
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
        subagent_config = subagent_config_for_call(call_id)
        project_id = str(subagent_config.flowhunt_project_id or runtime_settings.flowhunt_project_id or "")
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
        if subagent_config.complex_backend == "flow":
            return await tool_invoke_flowhunt_flow({**args, "message": description})
        prompt_meta = subagent_prompt_metadata(call_id, "flowhunt_project", input_text=description)
        if not args.get("suppress_progress"):
            schedule_tool_progress(
                call_id,
                prompt_meta["before_call_text"],
                progress_key=f"{call_id}:{response_to_event_id or project_id}:initial",
                min_interval_seconds=1.0,
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
            workspace_id=subagent_config.flowhunt_workspace_id or runtime_settings.flowhunt_workspace_id,
            base_url=runtime_settings.flowhunt_base_url,
            timeout=runtime_settings.flowhunt_timeout,
        )
        description_for_subagent = input_text_for_subagent(call_id, description)
        result = await run_flowhunt_issue_with_progress(
            client,
            project_id,
            title,
            description_for_subagent,
            {"call_id": call_id, "response_to_event_id": response_to_event_id, **prompt_meta},
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
                    prompt_meta,
                )
            )
        return {"event": event_to_dict(completed), "ok": result.ok, "message": result.message}

    async def tool_delegate_to_subagent(args: dict[str, Any]) -> dict[str, Any]:
        if subagent_coordinator is None or subagent_lifecycle is None:
            raise HTTPException(status_code=503, detail="Subagent coordinator is not configured")
        call_id = require_arg(args, "call_id")
        message = str(require_arg(args, "message")).strip()
        provider = str(require_arg(args, "provider")).strip()
        if not message:
            raise HTTPException(status_code=400, detail="message is required")
        if provider not in subagent_coordinator.provider_descriptors:
            raise HTTPException(status_code=400, detail=f"unknown subagent provider: {provider}")
        metadata = args.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise HTTPException(status_code=400, detail="metadata must be an object")
        response_to_event_id = args.get("response_to_event_id")
        duplicate = existing_flowhunt_request(call_id, response_to_event_id)
        if duplicate is not None:
            tracker.mark_responded(response_to_event_id)
            return {
                "event": event_to_dict(duplicate),
                "ok": True,
                "message": "A colleague is already checking this request.",
                "duplicate": True,
            }
        scope = subagent_scope_from_call(call_id)
        prompt_meta = subagent_prompt_metadata(call_id, provider, input_text=message)
        subagent_message = input_text_for_subagent(call_id, message)
        if not args.get("suppress_progress"):
            schedule_tool_progress(
                call_id,
                prompt_meta["before_call_text"],
                progress_key=f"{call_id}:{response_to_event_id or provider}:initial",
                min_interval_seconds=1.0,
            )
        tracker.mark_responded(response_to_event_id)
        requested = events.append(
            call_id,
            "subagent_task_requested",
            {
                "workspace_id": scope["workspace_id"],
                "voicebot_id": scope["voicebot_id"],
                "session_id": scope["session_id"],
                "provider": provider,
                "response_to_event_id": response_to_event_id,
            },
        )
        await hub.broadcast(requested)
        try:
            task = subagent_coordinator.request(
                SubagentTaskRequest(
                    workspace_id=scope["workspace_id"],
                    voicebot_id=scope["voicebot_id"],
                    session_id=scope["session_id"],
                    request_event_id=requested.id,
                    provider=provider,  # type: ignore[arg-type]
                    input_text=subagent_message,
                    dedupe_key=str(args.get("dedupe_key") or response_to_event_id or requested.id),
                    metadata={**metadata, **prompt_meta},
                )
            )
            scheduled = subagent_lifecycle.schedule(task)
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {
            "event": event_to_dict(requested),
            "task": subagent_task_to_dict(scheduled),
            "ok": scheduled.status != "failed",
            "message": prompt_meta["after_call_text"],
        }

    async def tool_invoke_flowhunt_flow(args: dict[str, Any]) -> dict[str, Any]:
        call_id = require_arg(args, "call_id")
        subagent_config = subagent_config_for_call(call_id)
        flow_id = str(subagent_config.flowhunt_flow_id or runtime_settings.flowhunt_flow_id or "")
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
        prompt_meta = subagent_prompt_metadata(call_id, "flowhunt_flow", input_text=message)
        subagent_message = input_text_for_subagent(call_id, message)
        if not args.get("suppress_progress"):
            schedule_tool_progress(
                call_id,
                prompt_meta["before_call_text"],
                progress_key=f"{call_id}:{response_to_event_id or flow_id}:initial",
                min_interval_seconds=1.0,
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
            and (subagent_config.flowhunt_workspace_id or runtime_settings.flowhunt_workspace_id)
            and "flowhunt_flow" in subagent_coordinator.providers
        ):
            task = subagent_coordinator.request(
                SubagentTaskRequest(
                    workspace_id=subagent_config.flowhunt_workspace_id or runtime_settings.flowhunt_workspace_id,
                    session_id=call_id,
                    request_event_id=invoked.id,
                    provider="flowhunt_flow",
                    input_text=subagent_message,
                    dedupe_key=str(response_to_event_id or invoked.id),
                    metadata={
                        "response_to_event_id": response_to_event_id,
                        **prompt_meta,
                    },
                )
            )
            scheduled = subagent_lifecycle.schedule(task)
            if scheduled.is_terminal():
                await notify_subagent_terminal_task(scheduled)
            return {
                "event": event_to_dict(invoked),
                "task": subagent_task_to_dict(scheduled),
                "ok": scheduled.status != "failed",
                "message": prompt_meta["after_call_text"],
            }
        client = FlowHuntClient(
            api_key=runtime_settings.flowhunt_api_key,
            workspace_id=subagent_config.flowhunt_workspace_id or runtime_settings.flowhunt_workspace_id,
            base_url=runtime_settings.flowhunt_base_url,
            timeout=runtime_settings.flowhunt_timeout,
        )
        result = await asyncio.to_thread(
            client.invoke_flow_and_wait,
            flow_id,
            subagent_message,
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
                    prompt_meta,
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
                    prompt_meta,
                )
            )
        else:
            text = render_subagent_prompt(prompt_meta["subagent_prompts"]["result_prompt"], result=result.message, provider="flowhunt_flow", call_id=call_id, input_text=message, status="completed")
            await request_communication_agent(
                call_id,
                "colleague_result",
                text,
                response_to_event_id,
                flow_id=flow_id,
                session_id=extract_session_id(result.data),
                ok=result.ok,
                source_event_id=completed.id,
                consume_prompt=prompt_meta["subagent_prompts"]["result_prompt"] if prompt_meta["subagent_prompts_explicit"] else None,
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
                "subagent_task_requested",
                "subagent_task_updated",
                "subagent_task_completed",
                "subagent_task_failed",
            }:
                continue
            try:
                event_response_id = int(event.data.get("response_to_event_id"))
            except (TypeError, ValueError):
                continue
            if event_response_id == response_event_id:
                return event
        return None

    def subagent_scope_from_call(call_id: str) -> dict[str, str]:
        snapshot = registry.snapshot(call_id)
        identity = session_identity_from_snapshot(snapshot or {}) if snapshot is not None else None
        if identity is not None:
            require_workspace_access(identity["workspace_id"])
            return identity
        workspace_id = non_empty_str(runtime_settings.flowhunt_workspace_id)
        if workspace_id is None:
            raise HTTPException(status_code=400, detail="workspace_id is required for subagent delegation")
        require_workspace_access(workspace_id)
        return {
            "workspace_id": workspace_id,
            "voicebot_id": "default",
            "session_id": call_id,
            "call_id": call_id,
            "transport": "",
        }

    def speak_tool_progress_sync(call_id: str, text: str) -> None:
        session = registry.get(call_id)
        if session is None:
            return
        try:
            session.submit_agent_response(AgentResponse(call_id=call_id, text=normalize_progress_message(text)))
        except Exception as exc:
            events.append(call_id, "agent_response_dropped", {"reason": "progress_playback_failed", "error": str(exc)})

    def schedule_tool_progress(
        call_id: str,
        text: str,
        *,
        progress_key: str | None = None,
        min_interval_seconds: float | None = None,
    ) -> None:
        key = progress_key or f"{call_id}:tool-progress"
        normalized = normalize_progress_message(text)
        if not delegated_progress_memory.should_speak(
            key,
            normalized,
            min_interval_seconds=min_interval_seconds,
        ):
            events.append(
                call_id,
                "agent_response_dropped",
                {"reason": "duplicate_progress_suppressed", "progress_key": key, "text": normalized},
            )
            return
        threading.Thread(target=speak_tool_progress_sync, args=(call_id, normalized), daemon=True).start()

    def render_subagent_prompt(template: str, **values: Any) -> str:
        if not template:
            return ""
        try:
            return template.format_map(_SafeFormatDict({key: "" if value is None else value for key, value in values.items()}))
        except (KeyError, ValueError):
            return template

    def subagent_prompt_metadata(
        call_id: str,
        provider: str,
        *,
        input_text: str = "",
        result: str = "",
        status: str = "",
        error: str = "",
        task_id: str = "",
        external_task_id: str = "",
    ) -> dict[str, Any]:
        prompts, explicit = explicit_subagent_prompt_for_call(call_id, provider)
        values = {
            "provider": provider,
            "call_id": call_id,
            "input_text": input_text,
            "result": result,
            "status": status,
            "error": error,
            "task_id": task_id,
            "external_task_id": external_task_id,
        }
        rendered = {
            "before_call_text": render_subagent_prompt(prompts.before_call_prompt, **values),
            "after_call_text": render_subagent_prompt(prompts.after_call_prompt, **values),
            "result_text": render_subagent_prompt(prompts.result_prompt, **values),
        }
        return {
            "subagent_prompts": prompts.as_dict(),
            "subagent_prompts_explicit": explicit,
            **rendered,
        }

    def session_language_for_call(call_id: str) -> dict[str, Any]:
        return detected_session_language(events.list_events(call_id=call_id, limit=1000))

    def input_text_for_subagent(call_id: str, message: str) -> str:
        detail_instruction = (
            "When returning a result, include voice_summary for the short spoken answer and "
            "chat_details for the more detailed widget chat answer. Do not make voice_summary "
            "and chat_details the same text."
        )
        session_language = session_language_for_call(call_id)
        language = str(session_language.get("language") or "").strip().lower()
        if not language:
            return f"{message}\n\n{detail_instruction}"
        return (
            f"Caller language: {language}. Answer in this language unless the caller explicitly asks "
            f"for a different language.\n\nCaller request:\n{message}\n\n{detail_instruction}"
        )

    def subagent_result_text(task: SubagentTask, message: str, *, ok: bool) -> tuple[str, dict[str, Any]]:
        metadata_prompts = task.metadata.get("subagent_prompts")
        explicit = bool(task.metadata.get("subagent_prompts_explicit"))
        if isinstance(metadata_prompts, dict):
            prompts = SubagentPromptConfig(
                before_call_prompt=str(metadata_prompts.get("before_call_prompt") or SubagentPromptConfig().before_call_prompt),
                after_call_prompt=str(metadata_prompts.get("after_call_prompt") or SubagentPromptConfig().after_call_prompt),
                result_prompt=str(metadata_prompts.get("result_prompt") or SubagentPromptConfig().result_prompt),
            )
        else:
            prompts, explicit = explicit_subagent_prompt_for_call(task.session_id, task.provider)
        result_value = message if ok else f"The task could not finish: {message}"
        text = render_subagent_prompt(
            prompts.result_prompt,
            result=result_value,
            provider=task.provider,
            call_id=task.session_id,
            input_text=task.input_text,
            status=task.status,
            error=task.error or "",
            task_id=task.task_id,
            external_task_id=task.external_task_id or "",
        )
        data = {
            "subagent_prompts_explicit": explicit,
            "result_prompt": prompts.result_prompt,
        }
        if explicit:
            data["consume_prompt"] = prompts.result_prompt
        return text, data

    async def request_communication_agent(
        call_id: str,
        reason: str,
        text: str,
        response_to_event_id: int | None,
        **data: Any,
    ) -> None:
        if registry.get(call_id) is None:
            return
        if reason in {"colleague_result", "colleague_progress"} and "session_language" not in data:
            session_language = session_language_for_call(call_id)
            if session_language:
                data["session_language"] = session_language
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
                        "budget_seconds": runtime_settings.latency_budget_delegated_progress_seconds,
                        "source_event_id": source_event_id,
                        "event_id": requested.id,
                        "reason": reason,
                    },
                )
                await hub.broadcast(metric)
                budget = metric_latency_budget_seconds(runtime_settings, "colleague_result_to_agent_request_seconds")
                if budget is not None and elapsed > budget:
                    exceeded = events.append(
                        call_id,
                        "latency_budget_exceeded",
                        {
                            "metric_event_id": metric.id,
                            "name": "colleague_result_to_agent_request_seconds",
                            "value": elapsed,
                            "budget_seconds": budget,
                            "source_event_id": source_event_id,
                            "event_id": requested.id,
                            "reason": reason,
                        },
                    )
                    await hub.broadcast(exceeded)

    async def notify_subagent_terminal_task(task: SubagentTask, terminal_event: VoicebotEvent | None = None) -> None:
        if registry.get(task.session_id) is None:
            return
        if task.metadata.get("speculative") and task.metadata.get("speculative_status") != "confirmed":
            events.append(
                task.session_id,
                "subagent_task_updated",
                {
                    **task.event_context(),
                    "suppressed_result": True,
                    "reason": "speculative_task_not_confirmed",
                },
            )
            return
        if task.status == "completed":
            result = task.result
            message = result.content if result and result.content else result.summary if result else "The delegated task completed."
            text, prompt_data = subagent_result_text(task, message, ok=True)
            await request_communication_agent(
                task.session_id,
                "colleague_result",
                text,
                task.request_event_id,
                subagent_task_id=task.task_id,
                provider=task.provider,
                external_task_id=task.external_task_id,
                ok=True,
                source_event_id=terminal_event.id if terminal_event else None,
                **prompt_data,
                data=task.clean_result_context(),
            )
            return
        if task.status in {"failed", "timed_out"}:
            message = task.error or task.status
            text, prompt_data = subagent_result_text(task, message, ok=False)
            await request_communication_agent(
                task.session_id,
                "colleague_result",
                text,
                task.request_event_id,
                subagent_task_id=task.task_id,
                provider=task.provider,
                external_task_id=task.external_task_id,
                ok=False,
                source_event_id=terminal_event.id if terminal_event else None,
                **prompt_data,
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
                message = normalize_progress_message(
                    update or (f"Current status is {state_text}." if state_text else "Still in progress.")
                )
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
                next_progress = now + max(1.0, progress_update_seconds)

        response = latest.data.get("response")
        result = extract_issue_result(response)
        state = extract_issue_state(response)
        update = extract_issue_updates(response)
        if result:
            return FlowHuntResult(True, result, latest.data)
        if state and is_terminal_issue_state(state):
            ok = state not in {"failed", "error", "cancelled", "canceled", "human_input_needed"}
            message = normalize_progress_message(update or f"The colleague task finished with status {state}.")
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
        prompt_meta: dict[str, Any] | None = None,
    ) -> None:
        prompt_meta = prompt_meta or subagent_prompt_metadata(call_id, "flowhunt_project")
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
                    render_subagent_prompt(
                        str(prompt_meta["subagent_prompts"]["result_prompt"]),
                        result=f"The task could not finish: {latest.message}",
                        provider="flowhunt_project",
                        call_id=call_id,
                        status="failed",
                    ),
                    response_to_event_id,
                    project_id=project_id,
                    issue_id=issue_id,
                    ok=False,
                    source_event_id=completed.id,
                    consume_prompt=prompt_meta["subagent_prompts"]["result_prompt"] if prompt_meta.get("subagent_prompts_explicit") else None,
                    data=latest.data,
                )
                return

            response = latest.data.get("response")
            result = extract_issue_result(response)
            update = extract_issue_updates(response)
            state = extract_issue_state(response).lower()
            if result or is_terminal_issue_state(state):
                message = normalize_progress_message(result or update or f"The colleague task finished with status {state}.")
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
                    render_subagent_prompt(
                        str(prompt_meta["subagent_prompts"]["result_prompt"]),
                        result=message,
                        provider="flowhunt_project",
                        call_id=call_id,
                        status=state or "completed",
                    ),
                    response_to_event_id,
                    project_id=project_id,
                    issue_id=issue_id,
                    ok=state not in {"failed", "error", "cancelled", "canceled", "human_input_needed"},
                    source_event_id=completed.id,
                    consume_prompt=prompt_meta["subagent_prompts"]["result_prompt"] if prompt_meta.get("subagent_prompts_explicit") else None,
                    data=latest.data,
                )
                return

            now = asyncio.get_running_loop().time()
            if now >= next_progress:
                message = normalize_progress_message(update or "The colleague task is still in progress.")
                if message == last_progress_message or not delegated_progress_memory.should_speak(
                    f"{call_id}:{issue_id}:progress",
                    message,
                    min_interval_seconds=progress_update_seconds,
                ):
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
                    render_subagent_prompt(
                        str(prompt_meta.get("after_call_text") or prompt_meta["subagent_prompts"]["after_call_prompt"]),
                        result=message,
                        provider="flowhunt_project",
                        call_id=call_id,
                        status="running",
                    ),
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
        prompt_meta: dict[str, Any] | None = None,
    ) -> None:
        prompt_meta = prompt_meta or subagent_prompt_metadata(call_id, "flowhunt_flow")
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
                    render_subagent_prompt(
                        str(prompt_meta["subagent_prompts"]["result_prompt"]),
                        result=latest.message,
                        provider="flowhunt_flow",
                        call_id=call_id,
                        status="completed",
                    ),
                    response_to_event_id,
                    flow_id=flow_id,
                    session_id=session_id,
                    ok=True,
                    source_event_id=completed.id,
                    consume_prompt=prompt_meta["subagent_prompts"]["result_prompt"] if prompt_meta.get("subagent_prompts_explicit") else None,
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
        prompt_meta: dict[str, Any] | None = None,
    ) -> None:
        prompt_meta = prompt_meta or subagent_prompt_metadata(call_id, "flowhunt_flow")
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
                    render_subagent_prompt(
                        str(prompt_meta["subagent_prompts"]["result_prompt"]),
                        result=f"The task could not finish: {latest.message}",
                        provider="flowhunt_flow",
                        call_id=call_id,
                        status="failed",
                    ),
                    response_to_event_id,
                    flow_id=flow_id,
                    task_id=task_id,
                    ok=False,
                    source_event_id=completed.id,
                    consume_prompt=prompt_meta["subagent_prompts"]["result_prompt"] if prompt_meta.get("subagent_prompts_explicit") else None,
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
                    render_subagent_prompt(
                        str(prompt_meta["subagent_prompts"]["result_prompt"]),
                        result=message,
                        provider="flowhunt_flow",
                        call_id=call_id,
                        status="completed" if ok else "failed",
                    ),
                    response_to_event_id,
                    flow_id=flow_id,
                    task_id=task_id,
                    ok=ok,
                    source_event_id=completed.id,
                    consume_prompt=prompt_meta["subagent_prompts"]["result_prompt"] if prompt_meta.get("subagent_prompts_explicit") else None,
                    data=latest.data,
                )
                return

    def tool_get_transcript(args: dict[str, Any]) -> dict[str, Any]:
        call_id = require_arg(args, "call_id")
        return call_transcript_payload(
            transcripts_api_context,
            call_id,
            after=optional_int_arg(args, "after", 0),
            limit=validated_limit(optional_int_arg(args, "limit", 200)),
        )

    def tool_list_transcripts(args: dict[str, Any]) -> dict[str, Any]:
        return list_transcripts_payload(transcripts_api_context)

    def tool_list_transcript_summaries(args: dict[str, Any]) -> dict[str, Any]:
        return transcript_summaries_payload(
            transcripts_api_context,
            after_call_id=args.get("after_call_id"),
            limit=validated_limit(optional_int_arg(args, "limit", 200)),
        )

    def tool_get_transcript_stats(args: dict[str, Any]) -> dict[str, Any]:
        return transcript_stats_payload(
            transcripts_api_context,
            after_call_id=args.get("after_call_id"),
            limit=validated_limit(optional_int_arg(args, "limit", 200)),
        )

    def tool_get_events(args: dict[str, Any]) -> dict[str, Any]:
        return events_payload(
            events_api_context,
            after=optional_int_arg(args, "after", 0),
            call_id=args.get("call_id"),
            limit=validated_limit(optional_int_arg(args, "limit", 200)),
        )

    def tool_get_metrics(args: dict[str, Any]) -> dict[str, Any]:
        return metrics_payload(events_api_context, call_id=args.get("call_id"))

    def tool_get_active_calls(args: dict[str, Any]) -> dict[str, Any]:
        return {"active_calls": registry.active_call_ids()}

    def tool_get_call_state(args: dict[str, Any]) -> dict[str, Any]:
        call_id = require_arg(args, "call_id")
        return call_state_payload(calls_api_context, call_id)

    def tool_get_runtime_config(args: dict[str, Any]) -> dict[str, Any]:
        return runtime_config_payload(runtime_api_context)

    def tool_get_agent_task_status(args: dict[str, Any]) -> dict[str, Any]:
        return agent_task_status_payload(agent_tasks_api_context, owner=args.get("owner"))

    def tool_get_agent_task_summary(args: dict[str, Any]) -> dict[str, Any]:
        return agent_task_summary_payload(
            agent_tasks_api_context,
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
    tool_executor.register("delegate_to_subagent", tool_delegate_to_subagent)
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

    app.include_router(create_agent_tools_router(AgentToolsApiContext(tool_executor=tool_executor)))
    app.include_router(
        create_subagents_router(
            SubagentsApiContext(
                subagent_coordinator=subagent_coordinator,
                subagent_lifecycle=subagent_lifecycle,
                require_workspace_access=require_workspace_access,
                notify_subagent_terminal_task=notify_subagent_terminal_task,
            )
        )
    )

    unclassified_routes = apply_route_audiences(app.routes)
    app.state.route_audience_issues = unclassified_routes
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


def durable_call_events(
    events: EventStore,
    transcripts: TranscriptStore,
    call_id: str,
    *,
    after: int = 0,
    limit: int = 200,
) -> list[VoicebotEvent]:
    merged: dict[int, VoicebotEvent] = {}
    for event in transcripts.read(call_id, after=after, limit=None):
        restored = transcript_event_to_voicebot_event(event)
        if restored is not None:
            merged[restored.id] = restored
    for event in events.list_events(after=after, call_id=call_id, limit=limit):
        merged[event.id] = event
    return sorted(merged.values(), key=lambda event: event.id)[:limit]


def transcript_event_to_voicebot_event(event: dict[str, Any]) -> VoicebotEvent | None:
    try:
        event_id = int(event["id"])
        call_id = str(event["call_id"]).strip()
        event_type = str(event["type"]).strip()
        timestamp = str(event["timestamp"]).strip()
    except (KeyError, TypeError, ValueError):
        return None
    if event_id < 1 or not call_id or not event_type or not timestamp:
        return None
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    return VoicebotEvent(event_id, call_id, event_type, timestamp, data)


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


DASHBOARD_PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>FlowHunt Voicebot Dashboard</title>
  <style>
    :root {
      --border:#d8dee4; --muted:#57606a; --bg:#f6f8fa; --panel:#ffffff;
      --text:#24292f; --accent:#0969da; --accent-bg:#ddf4ff; --danger:#cf222e;
    }
    * { box-sizing:border-box; }
    body { margin:0; font-family:system-ui,-apple-system,Segoe UI,sans-serif; color:var(--text); background:#fff; }
    header { display:flex; align-items:center; justify-content:space-between; gap:1rem; padding:.85rem 1.1rem; border-bottom:1px solid var(--border); background:var(--bg); }
    h1 { margin:0; font-size:1.05rem; }
    h2 { margin:0 0 .75rem; font-size:1rem; }
    h3 { margin:1rem 0 .5rem; font-size:.95rem; }
    main { display:grid; grid-template-columns:15rem minmax(0,1fr); min-height:calc(100vh - 3.4rem); }
    nav { border-right:1px solid var(--border); padding:.85rem; background:#fbfcfd; }
    nav button { display:block; width:100%; margin:0 0 .35rem; text-align:left; }
    nav button.active { border-color:var(--accent); background:var(--accent-bg); color:#0550ae; font-weight:700; }
    section { display:none; padding:1rem; min-width:0; }
    section.active { display:block; }
    button, select, input, textarea { font:inherit; }
    button { padding:.45rem .7rem; border:1px solid var(--border); border-radius:6px; background:#fff; cursor:pointer; }
    button:hover { border-color:var(--accent); }
    select, input, textarea { width:100%; padding:.45rem .55rem; border:1px solid var(--border); border-radius:6px; background:#fff; }
    textarea { min-height:5rem; resize:vertical; font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:.8rem; }
    table { width:100%; border-collapse:collapse; table-layout:fixed; font-size:.84rem; }
    th, td { padding:.55rem; border-bottom:1px solid #eaeef2; text-align:left; vertical-align:top; overflow-wrap:anywhere; }
    th { position:sticky; top:0; background:var(--bg); color:var(--muted); font-weight:700; z-index:1; }
    tr.clickable { cursor:pointer; }
    tr.clickable:hover { background:#f6f8fa; }
    pre { margin:0; padding:.75rem; max-height:24rem; overflow:auto; border:1px solid var(--border); border-radius:8px; background:#f6f8fa; color:#24292f; white-space:pre-wrap; font-size:.78rem; }
    iframe { width:100%; min-height:43rem; border:1px solid var(--border); border-radius:8px; background:#fff; }
    .muted { color:var(--muted); font-size:.85rem; }
    .toolbar { display:flex; align-items:end; gap:.75rem; margin:0 0 .85rem; flex-wrap:wrap; }
    .toolbar label { display:block; min-width:14rem; }
    .toolbar .grow { flex:1 1 18rem; }
    .table-filter { margin:0 0 .55rem; }
    .table-filter input { max-width:24rem; }
    .table-wrap { border:1px solid var(--border); border-radius:8px; overflow:auto; background:#fff; max-height:34rem; }
    .table-wrap.compact { max-height:24rem; }
    .split { display:grid; grid-template-columns:minmax(18rem,24rem) minmax(0,1fr); gap:1rem; align-items:start; }
    .panel { border:1px solid var(--border); border-radius:8px; padding:.85rem; background:var(--panel); }
    .tabs { display:flex; gap:.35rem; margin:.75rem 0; border-bottom:1px solid var(--border); }
    .tabs button { border-bottom:0; border-radius:6px 6px 0 0; }
    .tabs button.active { background:var(--accent-bg); color:#0550ae; border-color:#c9d7e8; font-weight:700; }
    .tab-panel { display:none; }
    .tab-panel.active { display:block; }
    .form-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:.7rem; }
    .form-grid label.full { grid-column:1 / -1; }
    .provider-editor-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:.75rem; }
    fieldset { border:1px solid var(--border); border-radius:8px; padding:.75rem; min-width:0; }
    fieldset label { display:block; margin:.45rem 0; }
    fieldset label.full textarea { min-height:6rem; }
    .inline-note { margin:.5rem 0 .75rem; padding:.5rem .65rem; border:1px solid #d8dee4; border-radius:8px; background:#f6f8fa; color:var(--muted); font-size:.85rem; }
    .badge { display:inline-block; padding:.1rem .45rem; border-radius:999px; background:#dafbe1; color:#1a7f37; font-weight:700; font-size:.75rem; }
    .badge.off { background:#ffebe9; color:var(--danger); }
    .session-header { display:flex; align-items:center; justify-content:space-between; gap:1rem; margin:.25rem 0 1rem; flex-wrap:wrap; }
    .session-title-block h2 { margin:0; }
    .session-summary { display:flex; flex-wrap:wrap; gap:.45rem; }
    .session-chip { display:inline-flex; align-items:center; gap:.25rem; padding:.18rem .5rem; border:1px solid var(--border); border-radius:999px; background:#f6f8fa; color:var(--muted); font-size:.78rem; font-weight:700; }
    .session-chip strong { color:var(--text); font-weight:700; }
    .session-layout { display:grid; grid-template-columns:1fr; gap:1rem; align-items:start; }
    .session-tabs { display:flex; gap:.35rem; margin:0 0 .85rem; border-bottom:1px solid var(--border); overflow:auto; }
    .session-tabs button { border-bottom:0; border-radius:6px 6px 0 0; white-space:nowrap; }
    .session-tabs button.active { background:var(--accent-bg); color:#0550ae; border-color:#c9d7e8; font-weight:700; }
    .session-tab-panel { display:none; }
    .session-tab-panel.active { display:block; }
    .panel-title { display:flex; align-items:baseline; justify-content:space-between; gap:.75rem; margin:0 0 .65rem; }
    .panel-title h3 { margin:0; }
    .event-table th, .event-table td, .transcript-table th, .transcript-table td { padding:.48rem .6rem; }
    .event-row { background:#fff; }
    .event-row:hover { background:#f6f8fa; }
    .event-detail-row td { padding:0 .6rem .65rem; background:#fbfcfd; }
    .event-json { max-height:14rem; border-color:#eaeef2; background:#f6f8fa; color:#24292f; }
    .json-key { color:#8250df; font-weight:700; }
    .json-value { color:#1a7f37; font-weight:700; }
    .json-string { color:#0a3069; font-weight:700; }
    .json-null { color:#6e7781; font-weight:700; }
    .event-type-pill { display:inline-block; max-width:100%; padding:.12rem .45rem; border:1px solid #c9d7e8; border-radius:999px; background:#ddf4ff; color:#0550ae; font-size:.75rem; font-weight:700; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .event-summary { color:#57606a; font-size:.8rem; line-height:1.35; }
    .transcript-speaker { display:inline-block; padding:.12rem .45rem; border-radius:999px; font-size:.75rem; font-weight:700; }
    .transcript-speaker.caller { color:#1a7f37; background:#dafbe1; }
    .transcript-speaker.voicebot { color:#8250df; background:#fbefff; }
    .transcript-text { line-height:1.4; }
    .session-chat-text { line-height:1.5; }
    .session-chat-text h2, .session-chat-text h3 { margin:.15rem 0 .4rem; font-size:.95rem; line-height:1.3; }
    .session-chat-text p { margin:.2rem 0 .45rem; }
    .session-chat-text ul { margin:.25rem 0 .55rem 1.15rem; padding:0; }
    .session-chat-text li { margin:.15rem 0; }
    .session-chat-text code { padding:.08rem .25rem; border-radius:4px; background:#eaeef2; font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:.75rem; }
    .session-chat-text a { color:var(--accent); text-decoration:underline; overflow-wrap:anywhere; }
    .audio-row { display:flex; align-items:center; gap:.75rem; margin:.25rem 0 .75rem; }
    .audio-row audio { width:100%; }
    .gantt-wrap { border:1px solid var(--border); border-radius:8px; overflow:auto; background:#fff; margin:0 0 1rem; }
    .gantt-chart { min-width:52rem; padding:.7rem .8rem .85rem; }
    .gantt-axis { position:relative; height:1.6rem; margin-left:12.5rem; border-bottom:1px solid var(--border); color:var(--muted); font-size:.75rem; }
    .gantt-tick { position:absolute; top:0; transform:translateX(-50%); white-space:nowrap; }
    .gantt-row { display:grid; grid-template-columns:12rem minmax(40rem,1fr); gap:.5rem; align-items:center; min-height:2rem; }
    .gantt-label { color:var(--muted); font-size:.78rem; font-weight:700; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .gantt-lane { position:relative; height:1.45rem; border-bottom:1px solid #eaeef2; background:linear-gradient(90deg,#f6f8fa 1px,transparent 1px); background-size:10% 100%; }
    .gantt-bar { position:absolute; top:.32rem; min-width:.4rem; height:.75rem; border:0; padding:0; border-radius:999px; background:#0969da; box-shadow:0 0 0 1px rgba(9,105,218,.2); cursor:pointer; }
    .gantt-bar:hover, .gantt-bar:focus { outline:2px solid #0969da; outline-offset:2px; }
    .gantt-bar.selected { box-shadow:0 0 0 3px rgba(9,105,218,.35); }
    .gantt-bar.marker { width:.5rem; height:.5rem; top:.45rem; border-radius:50%; }
    .gantt-bar.speech { background:#1a7f37; }
    .gantt-bar.stt { background:#8250df; }
    .gantt-bar.agent { background:#0969da; }
    .gantt-bar.tts, .gantt-bar.playback { background:#bf8700; }
    .gantt-bar.subagent { background:#cf222e; }
    .gantt-bar.call { box-shadow:0 0 0 1px rgba(31,35,40,.35); }
    .gantt-bar.system { box-shadow:0 0 0 1px rgba(87,96,106,.55); }
    .gantt-bar.control { box-shadow:0 0 0 1px rgba(207,34,46,.45); }
    .gantt-type-call-connected { background:#2da44e; }
    .gantt-type-call-ended { background:#57606a; }
    .gantt-type-call-control { background:#d1242f; }
    .gantt-type-speech { background:#1a7f37; }
    .gantt-type-transcript { background:#116329; }
    .gantt-type-stt { background:#8250df; }
    .gantt-type-agent-requested { background:#0969da; }
    .gantt-type-agent-received { background:#54aeef; }
    .gantt-type-agent-queued { background:#1f6feb; }
    .gantt-type-agent-error { background:#cf222e; }
    .gantt-type-tts { background:#bf8700; }
    .gantt-type-playback { background:#9a6700; }
    .gantt-type-subagent-requested { background:#cf222e; }
    .gantt-type-subagent-updated { background:#fb8f44; }
    .gantt-type-subagent-completed { background:#2da44e; }
    .gantt-type-subagent-failed { background:#a40e26; }
    .gantt-type-flowhunt { background:#e36209; }
    .gantt-type-recording { background:#6f42c1; }
    .gantt-type-security { background:#57606a; }
    .gantt-type-runtime { background:#6e7781; }
    .gantt-type-system { background:#8c959f; }
    .gantt-empty { padding:.75rem; color:var(--muted); }
    .event-dialog { width:min(58rem,calc(100vw - 2rem)); max-height:min(42rem,calc(100vh - 2rem)); padding:0; border:1px solid var(--border); border-radius:8px; box-shadow:0 18px 48px rgba(31,35,40,.22); }
    .event-dialog::backdrop { background:rgba(31,35,40,.42); }
    .event-dialog-body { padding:1rem; }
    .event-dialog-header { display:flex; align-items:flex-start; justify-content:space-between; gap:1rem; margin:0 0 .75rem; }
    .event-dialog-header h4 { margin:0; font-size:1rem; }
    .event-dialog-close { line-height:1; padding:.35rem .5rem; }
    .gantt-detail { margin:.75rem 0 0; }
    .gantt-detail h4 { margin:0 0 .45rem; font-size:.88rem; }
    .gantt-detail-meta { display:flex; flex-wrap:wrap; gap:.35rem; margin-bottom:.5rem; }
    .gantt-detail-meta span { padding:.12rem .45rem; border:1px solid var(--border); border-radius:999px; color:var(--muted); font-size:.75rem; background:#f6f8fa; }
    @media (max-width:980px) {
      main { grid-template-columns:1fr; }
      nav { display:flex; gap:.35rem; overflow:auto; border-right:0; border-bottom:1px solid var(--border); }
      nav button { width:auto; white-space:nowrap; margin:0; }
      .split, .session-layout, .form-grid, .provider-editor-grid { grid-template-columns:1fr; }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>FlowHunt Voicebot Dashboard</h1>
      <div class="muted">Internal operations, sessions, configuration, and local voicebot testing</div>
    </div>
    <button id="refresh" type="button">Refresh</button>
  </header>
  <main>
    <nav aria-label="Main menu">
      <button class="active" type="button" data-view="workspaces">Workspaces</button>
      <button type="button" data-view="history">Sessions History</button>
      <button type="button" data-view="test">Voicebot Test</button>
    </nav>

    <section id="view-workspaces" class="active">
      <div class="split">
        <div class="panel">
          <h2>Workspaces</h2>
          <div class="table-filter"><input data-table-filter="workspace-rows" placeholder="Filter workspaces"></div>
          <div class="table-wrap">
            <table aria-label="Workspaces">
              <thead><tr><th>workspace_id</th><th>name</th></tr></thead>
              <tbody id="workspace-rows"></tbody>
            </table>
          </div>
        </div>
        <div class="panel">
          <h2 id="workspace-title">Workspace detail</h2>
          <div class="muted" id="workspace-hint">Select a workspace to see its voicebots.</div>
          <h3>Voicebots</h3>
          <div class="table-filter"><input data-table-filter="voicebot-rows" placeholder="Filter voicebots"></div>
          <div class="table-wrap">
            <table aria-label="Voicebots">
              <thead><tr><th>voicebot_id</th><th>name</th><th>enabled</th><th>sessions</th></tr></thead>
              <tbody id="voicebot-rows"></tbody>
            </table>
          </div>
          <div id="voicebot-detail" class="panel" style="margin-top:1rem; display:none;">
            <h3>Voicebot detail</h3>
            <div class="tabs">
              <button type="button" class="active" data-detail-tab="settings">Settings</button>
              <button type="button" data-detail-tab="prompts">Prompts</button>
              <button type="button" data-detail-tab="providers">Providers</button>
              <button type="button" data-detail-tab="runtime">Runtime</button>
            </div>
            <div id="detail-settings" class="tab-panel active">
              <div class="form-grid">
                <label>voicebot_id<input id="detail-voicebot-id" readonly></label>
                <label>display_name<input id="detail-display-name"></label>
                <label>enabled<select id="detail-enabled"><option value="true">true</option><option value="false">false</option></select></label>
                <label class="full">metadata<textarea id="detail-metadata"></textarea></label>
              </div>
              <div class="toolbar"><button id="save-voicebot" type="button">Save voicebot</button><span class="muted" id="save-voicebot-status"></span></div>
            </div>
            <div id="detail-prompts" class="tab-panel">
              <div class="form-grid">
                <label>language<input id="prompt-language"></label>
                <label class="full">greeting<textarea id="prompt-greeting"></textarea></label>
                <label class="full">filler_message<textarea id="prompt-filler"></textarea></label>
                <label class="full">colleague_progress_message<textarea id="prompt-colleague-progress"></textarea></label>
                <label class="full">system_prompt<textarea id="prompt-system"></textarea></label>
                <label class="full">stt_prompt<textarea id="prompt-stt"></textarea></label>
                <label>chat_mode<select id="prompt-chat-mode"><option value="disabled">disabled</option><option value="mirror_voice">mirror_voice</option><option value="expanded_chat">expanded_chat</option><option value="chat_only_when_useful">chat_only_when_useful</option></select></label>
                <label class="full">chat_system_prompt<textarea id="prompt-chat-system"></textarea></label>
                <label class="full">chat_response_prompt<textarea id="prompt-chat-response"></textarea></label>
                <label class="full">chat_rich_content_prompt<textarea id="prompt-chat-rich-content"></textarea></label>
                <label class="full">subagent_prompts_json<textarea id="prompt-subagent-prompts"></textarea></label>
              </div>
              <div class="toolbar"><button id="save-prompts" type="button">Save prompts</button><span class="muted" id="save-prompts-status"></span></div>
            </div>
            <div id="detail-providers" class="tab-panel">
              <div class="provider-editor-grid">
                <fieldset>
                  <legend>STT</legend>
                  <label>provider<input id="provider-stt-provider" list="stt-provider-options"></label>
                  <label>model<input id="provider-stt-model" list="stt-model-options"></label>
                  <label>fallback_provider<input id="provider-stt-fallback" list="stt-provider-options"></label>
                  <label>secret_name<input id="provider-stt-secret"></label>
                  <label class="full">config_json<textarea id="provider-stt-config"></textarea></label>
                </fieldset>
                <fieldset>
                  <legend>TTS</legend>
                  <label>provider<input id="provider-tts-provider" list="tts-provider-options"></label>
                  <label>model<input id="provider-tts-model" list="tts-model-options"></label>
                  <label>voice<input id="provider-tts-voice"></label>
                  <label>fallback_provider<input id="provider-tts-fallback" list="tts-provider-options"></label>
                  <label>secret_name<input id="provider-tts-secret"></label>
                  <label class="full">config_json<textarea id="provider-tts-config"></textarea></label>
                </fieldset>
                <fieldset>
                  <legend>Agent</legend>
                  <label>provider<input id="provider-agent-provider" list="agent-provider-options"></label>
                  <label>model<input id="provider-agent-model" list="agent-model-options"></label>
                  <label>fallback_provider<input id="provider-agent-fallback" list="agent-provider-options"></label>
                  <label>secret_name<input id="provider-agent-secret"></label>
                  <label class="full">config_json<textarea id="provider-agent-config"></textarea></label>
                </fieldset>
              </div>
              <datalist id="stt-provider-options"></datalist>
              <datalist id="tts-provider-options"></datalist>
              <datalist id="agent-provider-options"></datalist>
              <datalist id="stt-model-options"></datalist>
              <datalist id="tts-model-options"></datalist>
              <datalist id="agent-model-options"></datalist>
              <div class="inline-note" id="provider-config-note"></div>
              <div class="toolbar"><button id="save-providers" type="button">Save providers</button><span class="muted" id="save-providers-status"></span></div>
              <pre id="provider-json">{}</pre>
            </div>
            <div id="detail-runtime" class="tab-panel">
              <div class="form-grid">
                <label>enabled<select id="runtime-enabled"><option value="true">true</option><option value="false">false</option></select></label>
                <label class="full">realtime_json<textarea id="runtime-realtime"></textarea></label>
                <label class="full">channels_json<textarea id="runtime-channels"></textarea></label>
                <label class="full">quotas_json<textarea id="runtime-quotas"></textarea></label>
                <label class="full">subagents_json<textarea id="runtime-subagents"></textarea></label>
              </div>
              <div class="inline-note" id="runtime-config-note"></div>
              <div class="toolbar"><button id="save-runtime" type="button">Save runtime</button><span class="muted" id="save-runtime-status"></span></div>
              <pre id="runtime-json">{}</pre>
            </div>
          </div>
        </div>
      </div>
    </section>

    <section id="view-history">
      <h2>Sessions History</h2>
      <div class="toolbar">
        <label class="grow">Search<input data-table-filter="history-session-rows" placeholder="Filter sessions"></label>
        <label>Status<select id="history-status-filter"><option value="">All statuses</option></select></label>
      </div>
      <div class="table-wrap">
        <table aria-label="Sessions history">
          <thead><tr><th>workspace_id</th><th>voicebot_id</th><th>session_id</th><th>status</th><th>datetime started</th><th>length</th></tr></thead>
          <tbody id="history-session-rows"></tbody>
        </table>
      </div>
    </section>

    <section id="view-session">
      <div class="session-header">
        <div class="session-title-block">
          <button type="button" data-view-back>Back</button>
          <h2 id="session-title">Session</h2>
        </div>
        <div id="session-summary" class="session-summary"></div>
      </div>
      <div class="session-layout">
        <div class="session-tabs" role="tablist" aria-label="Session detail sections">
          <button type="button" class="active" data-session-tab="timeline">Timeline</button>
          <button type="button" data-session-tab="recording">Recording</button>
          <button type="button" data-session-tab="transcript">Transcript</button>
          <button type="button" data-session-tab="chat">Chat Widget</button>
          <button type="button" data-session-tab="events">Events</button>
        </div>
        <div id="session-tab-timeline" class="panel session-tab-panel active">
          <div class="panel-title"><h3>Timeline</h3><span class="muted">Click a bar to inspect details</span></div>
          <div id="session-gantt" class="gantt-wrap"></div>
        </div>
        <div id="session-tab-recording" class="panel session-tab-panel">
          <div class="panel-title"><h3>Recording</h3></div>
          <div id="session-recording" class="muted">No recording loaded.</div>
        </div>
        <div id="session-tab-transcript" class="panel session-tab-panel">
          <div class="panel-title"><h3>Transcript</h3><span class="muted" id="session-transcript-count"></span></div>
          <div class="table-filter"><input data-table-filter="session-transcript-rows" placeholder="Filter transcript"></div>
          <div class="table-wrap compact">
            <table aria-label="Session transcript" class="transcript-table">
              <thead><tr><th style="width:7rem;">Time</th><th style="width:6rem;">Speaker</th><th>Text</th></tr></thead>
              <tbody id="session-transcript-rows"></tbody>
            </table>
          </div>
        </div>
        <div id="session-tab-chat" class="panel session-tab-panel">
          <div class="panel-title"><h3>Chat Widget Communication</h3><span class="muted" id="session-chat-count"></span></div>
          <div class="table-filter"><input data-table-filter="session-chat-rows" placeholder="Filter chat widget communication"></div>
          <div class="table-wrap">
            <table aria-label="Session chat widget communication" class="transcript-table">
              <thead><tr><th style="width:7rem;">Time</th><th style="width:8rem;">Sender</th><th>Message</th></tr></thead>
              <tbody id="session-chat-rows"></tbody>
            </table>
          </div>
        </div>
        <div id="session-tab-events" class="panel session-tab-panel">
          <div class="panel-title"><h3>Events</h3><span class="muted" id="session-event-count"></span></div>
          <div class="table-filter"><input data-table-filter="session-event-rows" placeholder="Filter events"></div>
          <div class="table-wrap">
            <table aria-label="Session events" class="event-table">
              <thead><tr><th style="width:7rem;">Time</th><th style="width:5rem;">ID</th><th style="width:13rem;">Type</th><th>Summary</th></tr></thead>
              <tbody id="session-event-rows"></tbody>
            </table>
          </div>
        </div>
      </div>
    </section>

    <dialog id="session-gantt-dialog" class="event-dialog">
      <div class="event-dialog-body">
        <div class="event-dialog-header">
          <h4 id="session-gantt-dialog-title">Timeline event</h4>
          <button id="session-gantt-dialog-close" class="event-dialog-close" type="button" aria-label="Close timeline event details">Close</button>
        </div>
        <div id="session-gantt-detail" class="gantt-detail"></div>
      </div>
    </dialog>

    <section id="view-test">
      <h2>Voicebot Test</h2>
      <div class="toolbar">
        <label class="grow">Workspace<select id="test-workspace"></select></label>
        <label class="grow">Voicebot<select id="test-voicebot"></select></label>
      </div>
      <iframe id="webrtc-console" srcdoc="__WEBRTC_CONSOLE_SRCDOC__" title="Voicebot WebRTC test console"></iframe>
    </section>
  </main>
  <script>
    let state = null;
    let selectedWorkspaceId = "";
    let selectedVoicebotId = "";
    let previousView = "history";
    let currentRuntimeConfig = null;
    let currentProviderConfig = null;
    let providerCatalog = null;
    let currentSessionRoute = null;
    const views = ["workspaces", "history", "session", "test"];
    let pendingRoute = dashboardRouteFromLocation();
    let suppressRouteUpdate = false;
    if (pendingRoute.workspace_id) selectedWorkspaceId = pendingRoute.workspace_id;
    if (pendingRoute.voicebot_id) selectedVoicebotId = pendingRoute.voicebot_id;

    document.getElementById("refresh").addEventListener("click", load);
    document.querySelectorAll("nav button[data-view]").forEach((button) => {
      button.addEventListener("click", () => showView(button.dataset.view));
    });
    document.querySelector("[data-view-back]").addEventListener("click", () => showView(previousView));
    document.getElementById("session-gantt-dialog-close").addEventListener("click", () => {
      document.getElementById("session-gantt-dialog").close();
    });
    document.getElementById("session-gantt-dialog").addEventListener("click", (event) => {
      if (event.target.id === "session-gantt-dialog") event.target.close();
    });
    document.querySelectorAll("[data-detail-tab]").forEach((button) => {
      button.addEventListener("click", () => showDetailTab(button.dataset.detailTab));
    });
    document.querySelectorAll("[data-session-tab]").forEach((button) => {
      button.addEventListener("click", () => showSessionTab(button.dataset.sessionTab));
    });
    document.querySelectorAll("[data-table-filter]").forEach((input) => {
      input.addEventListener("input", () => applyTableFilter(input.dataset.tableFilter));
    });
    document.getElementById("history-status-filter").addEventListener("change", () => applyTableFilter("history-session-rows"));
    document.getElementById("save-voicebot").addEventListener("click", saveVoicebot);
    document.getElementById("save-prompts").addEventListener("click", savePrompts);
    document.getElementById("save-providers").addEventListener("click", saveProviders);
    document.getElementById("save-runtime").addEventListener("click", saveRuntimeConfig);
    document.getElementById("test-workspace").addEventListener("change", () => {
      selectedWorkspaceId = document.getElementById("test-workspace").value;
      renderTestVoicebots();
      updateDashboardUrl({view: "test", workspace_id: selectedWorkspaceId, voicebot_id: selectedVoicebotId});
      load();
    });
    document.getElementById("test-voicebot").addEventListener("change", () => {
      selectedVoicebotId = document.getElementById("test-voicebot").value;
      updateDashboardUrl({view: "test", workspace_id: selectedWorkspaceId, voicebot_id: selectedVoicebotId});
      postTestTarget();
    });
    document.getElementById("webrtc-console").addEventListener("load", postTestTarget);
    window.addEventListener("hashchange", handleDashboardLocationChange);
    window.addEventListener("popstate", handleDashboardLocationChange);
    function handleDashboardLocationChange() {
      pendingRoute = dashboardRouteFromLocation();
      selectedWorkspaceId = pendingRoute.workspace_id || "";
      selectedVoicebotId = pendingRoute.voicebot_id || "";
      load(false);
    }
    load();
    setInterval(() => {
      if (["history", "test"].includes(currentView())) load(false);
    }, 5000);

    async function load(showErrors = true) {
      const qs = selectedWorkspaceId ? `?workspace_id=${encodeURIComponent(selectedWorkspaceId)}` : "";
      try {
        if (!providerCatalog) providerCatalog = await fetchJson("/providers");
        const response = await fetch(`/dashboard/state${qs}`);
        if (!response.ok) throw new Error(await response.text());
        state = await response.json();
        selectedWorkspaceId = state.selected_workspace_id || selectedWorkspaceId || "";
        selectedVoicebotId = selectedVoicebotId || ((state.voicebots || [])[0] || {}).voicebot_id || "";
        renderAll();
        if (pendingRoute) {
          const route = pendingRoute;
          pendingRoute = null;
          await applyDashboardRoute(route);
        }
      } catch (error) {
        if (showErrors) alert(`Dashboard load failed: ${error}`);
      }
    }

    function renderAll() {
      renderWorkspaces();
      renderVoicebots();
      renderSessions("history-session-rows", state.session_history || []);
      renderTestSelectors();
      applyAllTableFilters();
    }

    function renderWorkspaces() {
      const tbody = document.getElementById("workspace-rows");
      tbody.innerHTML = "";
      for (const workspace of state.workspace_rows || []) {
        const row = tbody.insertRow();
        row.className = "clickable";
        row.insertCell().textContent = workspace.workspace_id || "";
        row.insertCell().textContent = workspace.name || workspace.workspace_id || "";
        row.onclick = () => {
          selectedWorkspaceId = workspace.workspace_id;
          selectedVoicebotId = "";
          updateDashboardUrl({view: "workspaces", workspace_id: selectedWorkspaceId});
          load();
        };
      }
      document.getElementById("workspace-title").textContent = selectedWorkspaceId ? `Workspace ${selectedWorkspaceId}` : "Workspace detail";
      document.getElementById("workspace-hint").textContent = selectedWorkspaceId ? "Open a voicebot to edit settings and prompts." : "Select a workspace to see its voicebots.";
      applyTableFilter("workspace-rows");
    }

    function renderVoicebots() {
      const tbody = document.getElementById("voicebot-rows");
      tbody.innerHTML = "";
      for (const bot of state.voicebots || []) {
        const row = tbody.insertRow();
        row.className = "clickable";
        row.insertCell().textContent = bot.voicebot_id || "";
        row.insertCell().textContent = bot.display_name || "";
        row.insertCell().innerHTML = `<span class="badge ${bot.enabled ? "" : "off"}">${bot.enabled ? "enabled" : "disabled"}</span>`;
        row.insertCell().textContent = bot.active_sessions || 0;
        row.onclick = () => openVoicebot(bot);
      }
      applyTableFilter("voicebot-rows");
    }

    async function openVoicebot(bot, options = {}) {
      selectedWorkspaceId = bot.workspace_id;
      selectedVoicebotId = bot.voicebot_id;
      if (options.updateUrl !== false) {
        updateDashboardUrl({view: "workspaces", workspace_id: selectedWorkspaceId, voicebot_id: selectedVoicebotId});
      }
      document.getElementById("voicebot-detail").style.display = "block";
      document.getElementById("detail-voicebot-id").value = bot.voicebot_id || "";
      document.getElementById("detail-display-name").value = bot.display_name || "";
      document.getElementById("detail-enabled").value = String(Boolean(bot.enabled));
      document.getElementById("detail-metadata").value = JSON.stringify(bot.metadata || {}, null, 2);
      await loadRuntimeConfig();
      await Promise.all([loadPrompts(), loadProviderConfig()]);
    }

    async function loadPrompts() {
      const target = `/workspaces/${encodeURIComponent(selectedWorkspaceId)}/voicebots/${encodeURIComponent(selectedVoicebotId)}/prompts`;
      const payload = await fetchJson(target).catch(() => ({}));
      const prompts = payload.prompts || {};
      document.getElementById("prompt-language").value = prompts.language || "";
      document.getElementById("prompt-greeting").value = prompts.greeting || "";
      document.getElementById("prompt-filler").value = prompts.filler_message || "";
      document.getElementById("prompt-colleague-progress").value = prompts.colleague_progress_message || "";
      document.getElementById("prompt-system").value = prompts.system_prompt || "";
      document.getElementById("prompt-stt").value = prompts.stt_prompt || "";
      document.getElementById("prompt-chat-mode").value = prompts.chat?.mode || "disabled";
      document.getElementById("prompt-chat-system").value = prompts.chat?.system_prompt || "";
      document.getElementById("prompt-chat-response").value = prompts.chat?.response_prompt || "";
      document.getElementById("prompt-chat-rich-content").value = prompts.chat?.rich_content_prompt || "";
    }

    async function loadProviderConfig() {
      const target = `/workspaces/${encodeURIComponent(selectedWorkspaceId)}/voicebots/${encodeURIComponent(selectedVoicebotId)}/providers`;
      const payload = await fetchJson(target).catch((error) => ({missing: true, error: String(error)}));
      currentProviderConfig = payload.config || currentRuntimeConfig?.providers || defaultProviderConfig();
      renderProviderEditor(currentProviderConfig || defaultProviderConfig());
      document.getElementById("provider-config-note").textContent = payload.config
        ? "Provider config is saved for this voicebot."
        : "Provider config is not saved yet. Defaults are shown and will be created when you save.";
      document.getElementById("provider-json").textContent = JSON.stringify(payload, null, 2);
    }

    async function loadRuntimeConfig() {
      const target = `/workspaces/${encodeURIComponent(selectedWorkspaceId)}/voicebots/${encodeURIComponent(selectedVoicebotId)}/runtime-config`;
      const payload = await fetchJson(target).catch((error) => ({missing: true, error: String(error)}));
      currentRuntimeConfig = payload.config || null;
      renderRuntimeEditor(currentRuntimeConfig || defaultRuntimeConfig());
      document.getElementById("runtime-config-note").textContent = payload.config
        ? "Runtime config is saved for this voicebot."
        : "Runtime config is not saved yet. Defaults are shown and will be created when you save.";
      document.getElementById("prompt-subagent-prompts").value = JSON.stringify(
        (currentRuntimeConfig || defaultRuntimeConfig()).subagents?.prompts || {},
        null,
        2
      );
      document.getElementById("runtime-json").textContent = JSON.stringify(payload, null, 2);
    }

    function defaultProviderConfig() {
      return {
        workspace_id: selectedWorkspaceId,
        voicebot_id: selectedVoicebotId,
        stt: providerDefaults("stt"),
        tts: providerDefaults("tts"),
        agent: providerDefaults("agent"),
      };
    }

    function providerDefaults(family) {
      if (family === "stt") return {family, provider: "whisper", model: null, voice: null, secret_ref: null, fallback_provider: null, config: {}};
      if (family === "tts") return {family, provider: "supertonic", model: "supertonic-3", voice: "M1", secret_ref: null, fallback_provider: null, config: {}};
      return {family, provider: "openai-responses", model: "gpt-4.1-nano", voice: null, secret_ref: null, fallback_provider: null, config: {}};
    }

    function defaultRuntimeConfig() {
      return {
        enabled: true,
        providers: currentProviderConfig || defaultProviderConfig(),
        prompts: currentPromptPayload(),
        realtime: {
          silence_ms: 450,
          vad_start_ms: 60,
          min_seconds: 0.35,
          max_seconds: 20.0,
          start_threshold: 0.020,
          stop_threshold: 0.010,
          barge_in_threshold: 0.08,
          echo_tail_ms: 300,
          max_reply_chars: 240,
          tts_chunk_chars: 90,
        },
        channels: {
          voice_enabled: true,
          chat_enabled: false,
          chat_input_enabled: false,
          transcript_visible: false,
          rich_content_enabled: false,
        },
        quotas: {
          max_concurrent_sessions: 1,
          max_provider_inflight: 10,
          enabled_actions: ["say", "hangup_call", "transfer_call", "send_dtmf", "delegate_to_subagent", "invoke_flowhunt_flow"],
        },
        subagents: {
          flowhunt_workspace_id: "",
          flowhunt_flow_id: "",
          flowhunt_project_id: "",
          complex_backend: "flow",
          prompts: {},
        },
      };
    }

    function renderProviderEditor(config) {
      renderProviderDatalists();
      for (const family of ["stt", "tts", "agent"]) {
        const choice = config[family] || {};
        setValue(`provider-${family}-provider`, choice.provider || defaultProviderForFamily(family));
        setValue(`provider-${family}-model`, choice.model || "");
        if (family === "tts") setValue("provider-tts-voice", choice.voice || "");
        setValue(`provider-${family}-fallback`, choice.fallback_provider || "");
        setValue(`provider-${family}-secret`, choice.secret_ref?.name || "");
        setValue(`provider-${family}-config`, JSON.stringify(choice.config || {}, null, 2));
      }
    }

    function renderProviderDatalists() {
      if (!providerCatalog) return;
      for (const family of ["stt", "tts", "agent"]) {
        const catalog = providerCatalog[family] || {};
        const providers = catalog.supported || [];
        const providerList = document.getElementById(`${family}-provider-options`);
        providerList.innerHTML = providers.map((provider) => `<option value="${escapeHtml(provider)}"></option>`).join("");
        const modelSet = new Set();
        for (const descriptor of Object.values(catalog.capabilities || {})) {
          for (const model of descriptor.models || []) modelSet.add(model);
        }
        const modelList = document.getElementById(`${family}-model-options`);
        modelList.innerHTML = Array.from(modelSet).sort().map((model) => `<option value="${escapeHtml(model)}"></option>`).join("");
      }
    }

    function renderRuntimeEditor(config) {
      setValue("runtime-enabled", String(config.enabled !== false));
      setValue("runtime-realtime", JSON.stringify(config.realtime || {}, null, 2));
      setValue("runtime-channels", JSON.stringify(config.channels || defaultRuntimeConfig().channels, null, 2));
      setValue("runtime-quotas", JSON.stringify(config.quotas || {}, null, 2));
      setValue("runtime-subagents", JSON.stringify(config.subagents || {}, null, 2));
      if (config.subagents?.prompts) setValue("prompt-subagent-prompts", JSON.stringify(config.subagents.prompts, null, 2));
    }

    function defaultProviderForFamily(family) {
      if (family === "stt") return "whisper";
      if (family === "tts") return "supertonic";
      return "openai-responses";
    }

    function setValue(id, value) {
      const element = document.getElementById(id);
      if (element) element.value = value;
    }

    async function saveVoicebot() {
      const status = document.getElementById("save-voicebot-status");
      status.textContent = "Saving...";
      let metadata = {};
      try {
        metadata = JSON.parse(document.getElementById("detail-metadata").value || "{}");
      } catch (error) {
        status.textContent = "Invalid metadata JSON";
        return;
      }
      const payload = {
        display_name: document.getElementById("detail-display-name").value,
        enabled: document.getElementById("detail-enabled").value === "true",
        metadata
      };
      await fetchJson(`/workspaces/${encodeURIComponent(selectedWorkspaceId)}/voicebots/${encodeURIComponent(selectedVoicebotId)}`, {
        method: "PATCH",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload)
      });
      status.textContent = "Saved";
      await load(false);
    }

    async function savePrompts() {
      const status = document.getElementById("save-prompts-status");
      status.textContent = "Saving...";
      const payload = {
        language: document.getElementById("prompt-language").value,
        greeting: document.getElementById("prompt-greeting").value,
        filler_message: document.getElementById("prompt-filler").value,
        colleague_progress_message: document.getElementById("prompt-colleague-progress").value,
        system_prompt: document.getElementById("prompt-system").value,
        stt_prompt: document.getElementById("prompt-stt").value,
        chat: {
          mode: document.getElementById("prompt-chat-mode").value,
          system_prompt: document.getElementById("prompt-chat-system").value,
          response_prompt: document.getElementById("prompt-chat-response").value,
          rich_content_prompt: document.getElementById("prompt-chat-rich-content").value,
        }
      };
      await fetchJson(`/workspaces/${encodeURIComponent(selectedWorkspaceId)}/voicebots/${encodeURIComponent(selectedVoicebotId)}/prompts`, {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload)
      });
      if (currentRuntimeConfig) {
        let subagentPrompts = {};
        try {
          subagentPrompts = JSON.parse(document.getElementById("prompt-subagent-prompts").value || "{}");
        } catch (error) {
          status.textContent = "Main prompts saved; invalid subagent_prompts_json";
          return;
        }
        const runtimePayload = {
          providers: currentRuntimeConfig.providers,
          prompts: {...(currentRuntimeConfig.prompts || {}), ...payload},
          realtime: currentRuntimeConfig.realtime || {},
          channels: currentRuntimeConfig.channels || defaultRuntimeConfig().channels,
          quotas: currentRuntimeConfig.quotas || {},
          subagents: {
            ...(currentRuntimeConfig.subagents || {}),
            prompts: subagentPrompts,
          },
          enabled: currentRuntimeConfig.enabled !== false,
        };
        const runtimeResponse = await fetchJson(`/workspaces/${encodeURIComponent(selectedWorkspaceId)}/voicebots/${encodeURIComponent(selectedVoicebotId)}/runtime-config`, {
          method: "PUT",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(runtimePayload)
        });
        currentRuntimeConfig = runtimeResponse.config || currentRuntimeConfig;
        renderRuntimeEditor(currentRuntimeConfig || {});
        document.getElementById("runtime-json").textContent = JSON.stringify(runtimeResponse, null, 2);
      }
      status.textContent = "Saved";
    }

    async function saveProviders() {
      const status = document.getElementById("save-providers-status");
      status.textContent = "Saving...";
      let providers;
      try {
        providers = buildProviderPayloadFromEditor();
      } catch (error) {
        status.textContent = String(error.message || error);
        return;
      }
      const response = await fetchJson(`/workspaces/${encodeURIComponent(selectedWorkspaceId)}/voicebots/${encodeURIComponent(selectedVoicebotId)}/providers`, {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(providers),
      });
      currentProviderConfig = response.config || currentProviderConfig;
      document.getElementById("provider-json").textContent = JSON.stringify(response, null, 2);
      document.getElementById("provider-config-note").textContent = "Provider config is saved for this voicebot.";
      if (response.ok === false) {
        status.textContent = `Validation failed: ${(response.validation || []).map((item) => item.message).join("; ")}`;
        return;
      }
      if (currentRuntimeConfig) {
        const runtimeSaved = await saveRuntimeConfig({providers, statusElement: status, quiet: true});
        if (!runtimeSaved) return;
      }
      status.textContent = "Saved";
    }

    async function saveRuntimeConfig(options = {}) {
      const status = options.statusElement || document.getElementById("save-runtime-status");
      if (!options.quiet) status.textContent = "Saving...";
      let providers;
      let realtime;
      let quotas;
      let subagents;
      let channels;
      try {
        providers = options.providers || buildProviderPayloadFromEditor();
        realtime = parseJsonField("runtime-realtime", "realtime_json");
        channels = parseJsonField("runtime-channels", "channels_json");
        quotas = parseJsonField("runtime-quotas", "quotas_json");
        subagents = parseJsonField("runtime-subagents", "subagents_json");
      } catch (error) {
        status.textContent = String(error.message || error);
        return false;
      }
      const payload = {
        providers,
        prompts: currentPromptPayload(),
        realtime,
        channels,
        quotas,
        subagents,
        enabled: document.getElementById("runtime-enabled").value === "true",
      };
      const response = await fetchJson(`/workspaces/${encodeURIComponent(selectedWorkspaceId)}/voicebots/${encodeURIComponent(selectedVoicebotId)}/runtime-config`, {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload),
      });
      currentRuntimeConfig = response.config || currentRuntimeConfig;
      if (currentRuntimeConfig) {
        currentProviderConfig = currentRuntimeConfig.providers || currentProviderConfig;
        renderRuntimeEditor(currentRuntimeConfig);
        renderProviderEditor(currentProviderConfig || {});
        document.getElementById("runtime-config-note").textContent = "Runtime config is saved for this voicebot.";
      }
      document.getElementById("runtime-json").textContent = JSON.stringify(response, null, 2);
      if (response.ok === false) {
        status.textContent = `Validation failed: ${(response.validation || []).map((item) => item.message).join("; ")}`;
        return false;
      }
      if (!options.quiet) status.textContent = "Saved";
      return true;
    }

    function buildProviderPayloadFromEditor() {
      return {
        stt: providerChoiceFromEditor("stt"),
        tts: providerChoiceFromEditor("tts"),
        agent: providerChoiceFromEditor("agent"),
      };
    }

    function providerChoiceFromEditor(family) {
      const provider = document.getElementById(`provider-${family}-provider`).value.trim();
      const model = document.getElementById(`provider-${family}-model`).value.trim();
      const fallback = document.getElementById(`provider-${family}-fallback`).value.trim();
      const secret = document.getElementById(`provider-${family}-secret`).value.trim();
      const choice = {
        provider,
        model: model || null,
        secret_ref: secret ? {name: secret} : null,
        fallback_provider: fallback || null,
        config: parseJsonField(`provider-${family}-config`, `${family}_config_json`),
      };
      if (family === "tts") {
        const voice = document.getElementById("provider-tts-voice").value.trim();
        choice.voice = voice || null;
      }
      return choice;
    }

    function currentPromptPayload() {
      return {
        language: document.getElementById("prompt-language").value,
        greeting: document.getElementById("prompt-greeting").value,
        filler_message: document.getElementById("prompt-filler").value,
        colleague_progress_message: document.getElementById("prompt-colleague-progress").value,
        system_prompt: document.getElementById("prompt-system").value,
        stt_prompt: document.getElementById("prompt-stt").value,
        chat: {
          mode: document.getElementById("prompt-chat-mode").value,
          system_prompt: document.getElementById("prompt-chat-system").value,
          response_prompt: document.getElementById("prompt-chat-response").value,
          rich_content_prompt: document.getElementById("prompt-chat-rich-content").value,
        },
      };
    }

    function parseJsonField(id, label) {
      try {
        return JSON.parse(document.getElementById(id).value || "{}");
      } catch (error) {
        throw new Error(`Invalid ${label}`);
      }
    }

    function renderSessions(targetId, items) {
      const tbody = document.getElementById(targetId);
      tbody.innerHTML = "";
      if (targetId === "history-session-rows") renderHistoryStatusOptions(items);
      for (const item of items) {
        const row = tbody.insertRow();
        row.className = "clickable";
        row.dataset.status = item.status || "";
        row.insertCell().textContent = item.workspace_id || "";
        row.insertCell().textContent = item.voicebot_id || "";
        row.insertCell().textContent = item.session_id || "";
        row.insertCell().textContent = item.status || "";
        row.insertCell().textContent = formatDate(item.started_at);
        row.insertCell().textContent = sessionLength(item);
        row.onclick = () => openSession(item, "history");
      }
      applyTableFilter(targetId);
    }

    function renderHistoryStatusOptions(items) {
      const select = document.getElementById("history-status-filter");
      const selected = select.value;
      const statuses = [...new Set(items.map((item) => item.status || "").filter(Boolean))].sort();
      select.innerHTML = '<option value="">All statuses</option>';
      for (const status of statuses) {
        const option = document.createElement("option");
        option.value = status;
        option.textContent = status;
        select.appendChild(option);
      }
      select.value = statuses.includes(selected) ? selected : "";
    }

    async function openSession(item, fromView, options = {}) {
      previousView = fromView;
      const sessionTab = options.sessionTab || "timeline";
      currentSessionRoute = {
        view: "session",
        workspace_id: item.workspace_id || selectedWorkspaceId,
        voicebot_id: item.voicebot_id || selectedVoicebotId,
        session_id: item.session_id || "",
        from: fromView,
      };
      if (options.updateUrl !== false) {
        updateDashboardUrl({
          ...currentSessionRoute,
          session_tab: sessionTab,
        });
      }
      showView("session", {updateUrl: false});
      showSessionTab(sessionTab, {updateUrl: false});
      document.getElementById("session-title").textContent = `Session ${item.session_id}`;
      renderSessionSummary(item);
      const base = `/workspaces/${encodeURIComponent(item.workspace_id)}/voicebots/${encodeURIComponent(item.voicebot_id)}/sessions/${encodeURIComponent(item.session_id)}`;
      const [timeline, transcript] = await Promise.all([
        fetchJson(`${base}/timeline?limit=300`).catch((error) => ({events: [], error: String(error)})),
        fetchJson(`${base}/transcript?limit=300`).catch((error) => ({events: [], error: String(error)}))
      ]);
      renderSessionGantt(timeline.events || []);
      renderSessionEvents(timeline.events || []);
      renderSessionTranscript(transcript.events || []);
      renderSessionChat(timeline.events || []);
      renderRecording(item.external_session_id || item.session_id);
    }

    function renderSessionSummary(item) {
      const container = document.getElementById("session-summary");
      container.innerHTML = "";
      const values = [
        ["workspace", item.workspace_id || ""],
        ["voicebot", item.voicebot_id || ""],
        ["status", item.status || ""],
        ["length", sessionLength(item) || ""],
      ];
      for (const [label, value] of values) {
        if (!value) continue;
        const chip = document.createElement("span");
        chip.className = "session-chip";
        chip.innerHTML = `${escapeHtml(label)} <strong>${escapeHtml(value)}</strong>`;
        container.appendChild(chip);
      }
    }

    function renderSessionGantt(events) {
      const container = document.getElementById("session-gantt");
      container.innerHTML = "";
      const items = sessionGanttItems(events);
      if (!items.length) {
        const empty = document.createElement("div");
        empty.className = "gantt-empty";
        empty.textContent = "No timeline events available.";
        container.appendChild(empty);
        return;
      }
      const chart = document.createElement("div");
      chart.className = "gantt-chart";
      const start = Math.min(...items.map((item) => item.start));
      const end = Math.max(...items.map((item) => item.end));
      const span = Math.max(1, end - start);
      chart.appendChild(renderGanttAxis(span));
      for (const item of items) {
        const row = document.createElement("div");
        row.className = "gantt-row";
        const label = document.createElement("div");
        label.className = "gantt-label";
        label.textContent = `${item.lane} #${item.id}`;
        label.title = `${item.type} #${item.id}`;
        const laneNode = document.createElement("div");
        laneNode.className = "gantt-lane";
        const bar = document.createElement("button");
        const duration = Math.max(0, item.end - item.start);
        bar.className = `gantt-bar ${item.kind} ${item.typeClass}${duration < 0.05 ? " marker" : ""}`;
        bar.style.left = `${((item.start - start) / span) * 100}%`;
        bar.style.width = `${Math.max(0.5, (duration / span) * 100)}%`;
        bar.title = `${item.type} #${item.id} ${formatSeconds(item.start - start)}-${formatSeconds(item.end - start)}`;
        bar.type = "button";
        bar.setAttribute("aria-label", `Show ${item.type} event ${item.id}`);
        bar.onclick = () => showGanttEventDetails(item, bar);
        laneNode.appendChild(bar);
        row.appendChild(label);
        row.appendChild(laneNode);
        chart.appendChild(row);
      }
      container.appendChild(chart);
    }

    function showGanttEventDetails(item, selectedBar) {
      document.querySelectorAll(".gantt-bar.selected").forEach((node) => node.classList.remove("selected"));
      selectedBar.classList.add("selected");
      document.getElementById("session-gantt-dialog-title").textContent = `${item.type} #${item.id}`;
      const detail = document.getElementById("session-gantt-detail");
      detail.className = "gantt-detail";
      detail.innerHTML = "";
      const meta = document.createElement("div");
      meta.className = "gantt-detail-meta";
      for (const value of [item.lane, `${formatSeconds(item.start)} start`, `${formatSeconds(item.end - item.start)} duration`]) {
        const pill = document.createElement("span");
        pill.textContent = value;
        meta.appendChild(pill);
      }
      detail.appendChild(meta);
      detail.appendChild(renderJsonBlock(item.event));
      document.getElementById("session-gantt-dialog").showModal();
    }

    function renderGanttAxis(span) {
      const axis = document.createElement("div");
      axis.className = "gantt-axis";
      const tickCount = Math.min(6, Math.max(2, Math.ceil(span / 5) + 1));
      for (let index = 0; index < tickCount; index += 1) {
        const tick = document.createElement("div");
        tick.className = "gantt-tick";
        const ratio = tickCount === 1 ? 0 : index / (tickCount - 1);
        tick.style.left = `${ratio * 100}%`;
        tick.textContent = formatSeconds(span * ratio);
        axis.appendChild(tick);
      }
      return axis;
    }

    function sessionGanttItems(events) {
      const parsedAll = events
        .map((event) => ({event, time: Date.parse(event.timestamp || "")}))
        .filter((item) => Number.isFinite(item.time));
      if (!parsedAll.length) return [];
      const callEnded = parsedAll.find((item) => item.event.type === "call_ended");
      const callEndTime = callEnded?.time ?? null;
      const parsed = parsedAll.filter((item) => {
        if (isPostCallDashboardAuditEvent(item.event)) return false;
        return callEndTime === null || item.time <= callEndTime + 1000;
      });
      if (!parsed.length) return [];
      const firstMs = Math.min(...parsed.map((item) => item.time));
      return parsed.map(({event, time}) => {
        const start = (time - firstMs) / 1000;
        const duration = ganttEventDuration(event);
        return {
          id: event.id ?? "",
          type: event.type || "",
          lane: ganttEventLane(event),
          kind: ganttEventKind(event),
          typeClass: ganttEventTypeClass(event),
          event,
          start,
          end: start + duration,
        };
      });
    }

    function isPostCallDashboardAuditEvent(event) {
      const data = event.data || {};
      return event.type === "security_audit"
        && ["transcript_read", "recording_read", "timeline_read"].includes(String(data.action || ""));
    }

    function ganttEventDuration(event) {
      const data = event.data || {};
      const value = Number(data.duration_seconds ?? data.duration ?? data.elapsed ?? data.value);
      if (Number.isFinite(value) && value > 0 && value < 3600) return value;
      if (event.type === "agent_response_queued" || event.type === "tts_finished") {
        const duration = Number(data.duration);
        if (Number.isFinite(duration) && duration > 0) return duration;
      }
      return 0.02;
    }

    function ganttEventLane(event) {
      const type = String(event.type || "");
      if (type.includes("speech") || type === "user_transcript") return "Caller audio";
      if (type.startsWith("stt_")) return "STT";
      if (type.startsWith("agent_")) return "AI agent";
      if (type.startsWith("tts_")) return "TTS";
      if (type.startsWith("bot_playback")) return "Playback";
      if (type.startsWith("subagent_") || type.startsWith("flowhunt_")) return "Subagents";
      if (type.startsWith("call_control")) return "Call control";
      if (type.startsWith("call_")) return "Call lifecycle";
      if (type.startsWith("recording_")) return "Recording";
      if (type === "security_audit" || type.includes("config") || type.includes("updated")) return "System";
      return "System";
    }

    function ganttEventKind(event) {
      const lane = ganttEventLane(event);
      if (lane === "Caller audio") return "speech";
      if (lane === "STT") return "stt";
      if (lane === "AI agent") return "agent";
      if (lane === "TTS") return "tts";
      if (lane === "Playback") return "playback";
      if (lane === "Subagents") return "subagent";
      if (lane === "Call control") return "control";
      if (lane === "Call lifecycle") return "call";
      return "system";
    }

    function ganttEventTypeClass(event) {
      const type = String(event.type || "");
      if (type === "call_connected") return "gantt-type-call-connected";
      if (type === "call_ended") return "gantt-type-call-ended";
      if (type.startsWith("call_control")) return "gantt-type-call-control";
      if (type.includes("speech")) return "gantt-type-speech";
      if (type === "user_transcript") return "gantt-type-transcript";
      if (type.startsWith("stt_")) return "gantt-type-stt";
      if (type === "agent_response_requested") return "gantt-type-agent-requested";
      if (type === "agent_response_received") return "gantt-type-agent-received";
      if (type === "agent_response_queued") return "gantt-type-agent-queued";
      if (type.startsWith("agent_") && type.includes("error")) return "gantt-type-agent-error";
      if (type.startsWith("agent_")) return "gantt-type-agent-requested";
      if (type.startsWith("tts_")) return "gantt-type-tts";
      if (type.startsWith("bot_playback")) return "gantt-type-playback";
      if (type.startsWith("subagent_") && type.includes("completed")) return "gantt-type-subagent-completed";
      if (type.startsWith("subagent_") && type.includes("failed")) return "gantt-type-subagent-failed";
      if (type.startsWith("subagent_") && type.includes("updated")) return "gantt-type-subagent-updated";
      if (type.startsWith("subagent_")) return "gantt-type-subagent-requested";
      if (type.startsWith("flowhunt_")) return "gantt-type-flowhunt";
      if (type.startsWith("recording_")) return "gantt-type-recording";
      if (type === "security_audit") return "gantt-type-security";
      if (type.includes("config") || type.includes("updated")) return "gantt-type-runtime";
      return "gantt-type-system";
    }

    function formatSeconds(seconds) {
      return `${Math.max(0, seconds).toFixed(seconds >= 10 ? 0 : 1)}s`;
    }

    function renderSessionEvents(events) {
      const tbody = document.getElementById("session-event-rows");
      tbody.innerHTML = "";
      document.getElementById("session-event-count").textContent = `${events.length} events`;
      for (const event of events) {
        const row = tbody.insertRow();
        row.className = "event-row";
        row.insertCell().textContent = formatTimeOnly(event.timestamp);
        row.insertCell().textContent = event.id ?? "";
        const typeCell = row.insertCell();
        const type = document.createElement("span");
        type.className = "event-type-pill";
        type.textContent = event.type || "";
        type.title = event.type || "";
        typeCell.appendChild(type);
        const summary = row.insertCell();
        summary.className = "event-summary";
        summary.textContent = eventSummary(event);
        const detailRow = tbody.insertRow();
        detailRow.className = "event-detail-row";
        const detailCell = detailRow.insertCell();
        detailCell.colSpan = 4;
        const json = renderJsonBlock(event.data || {});
        json.classList.add("event-json");
        detailCell.appendChild(json);
      }
      applyTableFilter("session-event-rows");
    }

    function renderSessionTranscript(events) {
      const tbody = document.getElementById("session-transcript-rows");
      tbody.innerHTML = "";
      const rows = events.filter((event) => {
        const kind = String(event.data?.response_kind || "");
        return (event.type === "user_transcript" && event.data?.text)
          || (event.type === "agent_response_received" && event.data?.text && !["progress_ack", "stream_chunk"].includes(kind));
      });
      document.getElementById("session-transcript-count").textContent = `${rows.length} rows`;
      for (const event of rows) {
        const row = tbody.insertRow();
        row.insertCell().textContent = formatTimeOnly(event.timestamp);
        const speakerCell = row.insertCell();
        const speaker = document.createElement("span");
        const isCaller = event.type === "user_transcript";
        speaker.className = `transcript-speaker ${isCaller ? "caller" : "voicebot"}`;
        speaker.textContent = isCaller ? "Caller" : "Voicebot";
        speakerCell.appendChild(speaker);
        const textCell = row.insertCell();
        textCell.className = "transcript-text";
        textCell.textContent = event.data?.text || "";
      }
      applyTableFilter("session-transcript-rows");
    }

    function renderSessionChat(events) {
      const tbody = document.getElementById("session-chat-rows");
      tbody.innerHTML = "";
      const rows = events
        .filter((event) => isSessionChatEvent(event))
        .map((event) => sessionChatRow(event))
        .filter(Boolean);
      document.getElementById("session-chat-count").textContent = `${rows.length} rows`;
      for (const item of rows) {
        const row = tbody.insertRow();
        row.insertCell().textContent = formatTimeOnly(item.timestamp);
        const speakerCell = row.insertCell();
        const speaker = document.createElement("span");
        speaker.className = `transcript-speaker ${item.kind}`;
        speaker.textContent = item.sender;
        speakerCell.appendChild(speaker);
        const textCell = row.insertCell();
        textCell.className = "session-chat-text";
        if (item.markdown) textCell.innerHTML = renderSessionMarkdown(item.text);
        else textCell.textContent = item.text;
        if (item.blocks.length) {
          const detailRow = tbody.insertRow();
          const detailCell = detailRow.insertCell();
          detailCell.colSpan = 3;
          const json = renderJsonBlock(item.blocks);
          json.classList.add("event-json");
          detailCell.appendChild(json);
        }
      }
      applyTableFilter("session-chat-rows");
    }

    function isSessionChatEvent(event) {
      const kind = String(event.data?.response_kind || "");
      if (event.type === "user_transcript" && event.data?.text) return true;
      if (event.type === "user_transcript_partial" && event.data?.text) return true;
      if (event.type === "stt_no_text" || event.type === "stt_failed") return true;
      if (event.type === "agent_response_received" && !["progress_ack", "call_control_ack", "stream_chunk", "stream_finalized"].includes(kind)) {
        return Boolean(sessionChatTextForEvent(event));
      }
      return false;
    }

    function sessionChatRow(event) {
      const isCaller = event.type === "user_transcript" || event.type === "user_transcript_partial" || event.type === "stt_no_text" || event.type === "stt_failed";
      const text = isCaller ? sessionVisitorTextForEvent(event) : sessionChatTextForEvent(event);
      if (!text) return null;
      const chat = event.data?.chat;
      return {
        timestamp: event.timestamp,
        sender: isCaller ? "Visitor" : "Voicebot chat",
        kind: isCaller ? "caller" : "voicebot",
        text,
        markdown: !isCaller,
        blocks: !isCaller && Array.isArray(chat?.blocks) ? chat.blocks : [],
      };
    }

    function sessionVisitorTextForEvent(event) {
      if (event.type === "stt_no_text") return "No speech was recognized.";
      if (event.type === "stt_failed") return `Voice transcription failed. ${event.data?.error || "unknown error"}`;
      return String(event.data?.text || "");
    }

    function sessionChatTextForEvent(event) {
      const chat = event.data?.chat;
      if (chat && typeof chat === "object" && typeof chat.text === "string" && chat.text.trim()) {
        return chat.text;
      }
      return String(event.data?.text || "");
    }

    function renderSessionMarkdown(text) {
      const lines = String(text || "").split(/\\r?\\n/);
      const html = [];
      let listOpen = false;
      let paragraph = [];
      const flushParagraph = () => {
        if (!paragraph.length) return;
        html.push(`<p>${renderSessionInlineMarkdown(paragraph.join(" "))}</p>`);
        paragraph = [];
      };
      const closeList = () => {
        if (listOpen) html.push("</ul>");
        listOpen = false;
      };
      for (const rawLine of lines) {
        const line = rawLine.trim();
        if (!line) {
          flushParagraph();
          closeList();
          continue;
        }
        if (line.startsWith("### ")) {
          flushParagraph();
          closeList();
          html.push(`<h3>${renderSessionInlineMarkdown(line.slice(4))}</h3>`);
        } else if (line.startsWith("## ")) {
          flushParagraph();
          closeList();
          html.push(`<h2>${renderSessionInlineMarkdown(line.slice(3))}</h2>`);
        } else if (/^[-*]\\s+/.test(line)) {
          flushParagraph();
          if (!listOpen) {
            html.push("<ul>");
            listOpen = true;
          }
          html.push(`<li>${renderSessionInlineMarkdown(line.replace(/^[-*]\\s+/, ""))}</li>`);
        } else {
          paragraph.push(line);
        }
      }
      flushParagraph();
      closeList();
      return html.join("");
    }

    function renderSessionInlineMarkdown(text) {
      let html = escapeHtml(text);
      html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
      html = html.replace(/\\[([^\\]]+)\\]\\((https?:\\/\\/[^)\\s]+)\\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
      html = html.replace(/(https?:\\/\\/[^\\s<]+)/g, '<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>');
      html = html.replace(/\\*\\*([^*]+)\\*\\*/g, "<strong>$1</strong>");
      html = html.replace(/\\*([^*]+)\\*/g, "<em>$1</em>");
      return html;
    }

    function applyAllTableFilters() {
      document.querySelectorAll("[data-table-filter]").forEach((input) => applyTableFilter(input.dataset.tableFilter));
    }

    function applyTableFilter(tbodyId) {
      const input = document.querySelector(`[data-table-filter="${tbodyId}"]`);
      const tbody = document.getElementById(tbodyId);
      if (!input || !tbody) return;
      const query = normalizeFilterText(input.value);
      if (tbodyId === "session-event-rows") {
        for (let index = 0; index < tbody.rows.length; index += 2) {
          const summaryRow = tbody.rows[index];
          const detailRow = tbody.rows[index + 1];
          const match = !query || normalizeFilterText(`${summaryRow?.textContent || ""} ${detailRow?.textContent || ""}`).includes(query);
          if (summaryRow) summaryRow.hidden = !match;
          if (detailRow) detailRow.hidden = !match;
        }
        return;
      }
      for (const row of tbody.rows) {
        const statusFilter = tbodyId === "history-session-rows" ? document.getElementById("history-status-filter").value : "";
        const textMatches = !query || normalizeFilterText(row.textContent || "").includes(query);
        const statusMatches = !statusFilter || row.dataset.status === statusFilter;
        row.hidden = !textMatches || !statusMatches;
      }
    }

    function normalizeFilterText(value) {
      return String(value || "").toLowerCase().replace(/\\s+/g, " ").trim();
    }

    function eventSummary(event) {
      const data = event.data || {};
      if (data.text) return String(data.text);
      if (data.name) return `${data.name}${data.value !== undefined ? `: ${data.value}` : ""}`;
      if (data.action) return String(data.action);
      if (data.status) return String(data.status);
      if (data.reason) return String(data.reason);
      if (data.message) return String(data.message);
      const keys = Object.keys(data);
      return keys.length ? keys.slice(0, 4).join(", ") : "";
    }

    async function renderRecording(callId) {
      const node = document.getElementById("session-recording");
      node.textContent = "Checking recording...";
      try {
        const response = await fetch(`/calls/${encodeURIComponent(callId)}/recording`);
        if (response.status === 404) {
          node.textContent = "No recording is available for this session.";
          return;
        }
        if (!response.ok) throw new Error(await response.text());
        const payload = await response.json();
        node.innerHTML = `<div class="audio-row"><audio controls src="/calls/${escapeAttr(callId)}/recording.wav"></audio></div><pre>${escapeHtml(JSON.stringify(payload.metadata || {}, null, 2))}</pre>`;
      } catch (error) {
        node.textContent = `Recording load failed: ${error}`;
      }
    }

    function renderTestSelectors() {
      const workspaceSelect = document.getElementById("test-workspace");
      const previousWorkspace = workspaceSelect.value || selectedWorkspaceId;
      workspaceSelect.innerHTML = "";
      for (const workspace of state.workspace_rows || []) {
        const option = new Option(workspace.name || workspace.workspace_id, workspace.workspace_id);
        option.selected = workspace.workspace_id === previousWorkspace;
        workspaceSelect.appendChild(option);
      }
      selectedWorkspaceId = workspaceSelect.value || selectedWorkspaceId;
      renderTestVoicebots();
    }

    function renderTestVoicebots() {
      const select = document.getElementById("test-voicebot");
      const previous = select.value || selectedVoicebotId;
      select.innerHTML = "";
      for (const bot of state.voicebots || []) {
        const option = new Option(bot.display_name || bot.voicebot_id, bot.voicebot_id);
        option.selected = bot.voicebot_id === previous;
        select.appendChild(option);
      }
      selectedVoicebotId = select.value || selectedVoicebotId;
      postTestTarget();
    }

    function postTestTarget() {
      const frame = document.getElementById("webrtc-console");
      if (!frame.contentWindow || !selectedWorkspaceId || !selectedVoicebotId) return;
      frame.contentWindow.postMessage({
        type: "voicebot-test-target",
        workspace_id: selectedWorkspaceId,
        voicebot_id: selectedVoicebotId
      }, "*");
    }

    async function applyDashboardRoute(route) {
      const requestedView = route.view === "active" ? "history" : route.view;
      const routeView = views.includes(requestedView) ? requestedView : "workspaces";
      if (route.workspace_id && route.workspace_id !== selectedWorkspaceId) {
        selectedWorkspaceId = route.workspace_id;
        selectedVoicebotId = route.voicebot_id || "";
        pendingRoute = route;
        await load(false);
        return;
      }
      if (route.voicebot_id) selectedVoicebotId = route.voicebot_id;
      if (routeView === "session" && route.workspace_id && route.voicebot_id && route.session_id) {
        const item = [...(state.active_sessions || []), ...(state.session_history || [])]
          .find((session) => session.workspace_id === route.workspace_id
            && session.voicebot_id === route.voicebot_id
            && session.session_id === route.session_id) || {
              workspace_id: route.workspace_id,
              voicebot_id: route.voicebot_id,
              session_id: route.session_id,
              status: "unknown"
            };
        await openSession(item, route.from || "history", {updateUrl: false, sessionTab: route.session_tab || "timeline"});
        return;
      }
      showView(routeView, {updateUrl: false});
      if (route.voicebot_id) {
        const bot = (state.voicebots || []).find((entry) => entry.voicebot_id === route.voicebot_id);
        if (bot) await openVoicebot(bot, {updateUrl: false});
      }
      if (routeView === "test") postTestTarget();
    }

    function dashboardRouteFromLocation() {
      const params = new URLSearchParams(window.location.hash.replace(/^#/, ""));
      return {
        view: params.get("view") || "",
        workspace_id: params.get("workspace_id") || "",
        voicebot_id: params.get("voicebot_id") || "",
        session_id: params.get("session_id") || "",
        session_tab: params.get("session_tab") || "",
        from: params.get("from") || ""
      };
    }

    function updateDashboardUrl(route, options = {}) {
      if (suppressRouteUpdate) return;
      const params = new URLSearchParams();
      for (const key of ["view", "workspace_id", "voicebot_id", "session_id", "session_tab", "from"]) {
        if (route[key]) params.set(key, route[key]);
      }
      const nextUrl = `${window.location.pathname}${window.location.search}#${params.toString()}`;
      const method = options.replace ? "replaceState" : "pushState";
      window.history[method](null, "", nextUrl);
    }

    function routeForView(name) {
      const route = {view: name};
      if (selectedWorkspaceId) route.workspace_id = selectedWorkspaceId;
      if (selectedVoicebotId && ["workspaces", "test"].includes(name)) route.voicebot_id = selectedVoicebotId;
      return route;
    }

    function showView(name, options = {}) {
      for (const view of views) document.getElementById(`view-${view}`).classList.toggle("active", view === name);
      document.querySelectorAll("nav button[data-view]").forEach((button) => button.classList.toggle("active", button.dataset.view === name));
      if (options.updateUrl !== false) updateDashboardUrl(routeForView(name));
      if (name === "test") postTestTarget();
    }

    function currentView() {
      return views.find((view) => document.getElementById(`view-${view}`).classList.contains("active")) || "workspaces";
    }

    function showDetailTab(name) {
      document.querySelectorAll("[data-detail-tab]").forEach((button) => button.classList.toggle("active", button.dataset.detailTab === name));
      document.querySelectorAll(".tab-panel").forEach((panel) => panel.classList.remove("active"));
      document.getElementById(`detail-${name}`).classList.add("active");
    }

    function showSessionTab(name, options = {}) {
      const selected = ["timeline", "recording", "transcript", "chat", "events"].includes(name) ? name : "timeline";
      document.querySelectorAll("[data-session-tab]").forEach((button) => button.classList.toggle("active", button.dataset.sessionTab === selected));
      document.querySelectorAll(".session-tab-panel").forEach((panel) => panel.classList.remove("active"));
      document.getElementById(`session-tab-${selected}`).classList.add("active");
      if (options.updateUrl !== false && currentView() === "session" && currentSessionRoute) {
        updateDashboardUrl({...currentSessionRoute, session_tab: selected});
      }
    }

    async function fetchJson(url, options) {
      const response = await fetch(url, options);
      if (!response.ok) throw new Error(await response.text());
      return response.json();
    }

    function formatDate(value) {
      if (!value) return "";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return String(value);
      return date.toLocaleString();
    }

    function formatTimeOnly(value) {
      if (!value) return "";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return String(value);
      return date.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"});
    }

    function sessionLength(item) {
      if (!item.started_at) return "";
      const start = new Date(item.started_at);
      const end = item.ended_at ? new Date(item.ended_at) : new Date();
      if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return "";
      const seconds = Math.max(0, Math.floor((end - start) / 1000));
      const minutes = Math.floor(seconds / 60);
      const rest = seconds % 60;
      return `${minutes}:${String(rest).padStart(2, "0")}`;
    }

    function renderJsonBlock(value) {
      const pre = document.createElement("pre");
      appendJsonBlockValue(pre, value, 0);
      return pre;
    }

    function appendJsonBlockValue(parent, value, indent) {
      if (Array.isArray(value)) {
        parent.append("[");
        if (value.length) parent.append("\\n");
        value.forEach((item, index) => {
          parent.append(" ".repeat(indent + 2));
          appendJsonBlockValue(parent, item, indent + 2);
          if (index < value.length - 1) parent.append(",");
          parent.append("\\n");
        });
        if (value.length) parent.append(" ".repeat(indent));
        parent.append("]");
        return;
      }
      if (value && typeof value === "object") {
        const entries = Object.entries(value);
        parent.append("{");
        if (entries.length) parent.append("\\n");
        entries.forEach(([key, item], index) => {
          parent.append(" ".repeat(indent + 2));
          const keyNode = document.createElement("span");
          keyNode.className = "json-key";
          keyNode.textContent = JSON.stringify(key);
          parent.appendChild(keyNode);
          parent.append(": ");
          appendJsonBlockValue(parent, item, indent + 2);
          if (index < entries.length - 1) parent.append(",");
          parent.append("\\n");
        });
        if (entries.length) parent.append(" ".repeat(indent));
        parent.append("}");
        return;
      }
      const valueNode = document.createElement("span");
      if (typeof value === "string") {
        valueNode.className = "json-value json-string";
        valueNode.textContent = JSON.stringify(value);
      } else if (value === null) {
        valueNode.className = "json-null";
        valueNode.textContent = "null";
      } else {
        valueNode.className = "json-value";
        valueNode.textContent = JSON.stringify(value);
      }
      parent.appendChild(valueNode);
    }

    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, char => {
        if (char === "&") return "&amp;";
        if (char === "<") return "&lt;";
        if (char === ">") return "&gt;";
        if (char === '"') return "&quot;";
        return "&#39;";
      });
    }

    function escapeAttr(value) {
      return encodeURIComponent(String(value));
    }
  </script>
</body>
</html>"""


VOICEBOT_WIDGET_JS = r"""(() => {
  const currentScript = document.currentScript;
  const routeBase = new URL((currentScript && currentScript.src) || "/widget.js", window.location.href);
  const baseUrl = routeBase.origin;
  const routePath = currentScript && currentScript.dataset.voicebotRoute ? currentScript.dataset.voicebotRoute : "";
  const inline = currentScript && currentScript.dataset.inline === "true";
  const metadata = safeJson(currentScript && currentScript.dataset.visitorMetadata);
  let pc = null;
  let localStream = null;
  let sessionId = null;
  let started = false;

  const root = document.createElement("div");
  root.className = inline ? "fh-voicebot fh-voicebot-inline" : "fh-voicebot";
  root.innerHTML = `
    <button class="fh-voicebot-button" type="button" aria-live="polite">
      <span class="fh-voicebot-dot" aria-hidden="true"></span>
      <span class="fh-voicebot-label">Voice call</span>
    </button>
    <div class="fh-voicebot-panel" role="status" aria-live="polite">
      <div class="fh-voicebot-title">Voice call</div>
      <div class="fh-voicebot-status">Ready</div>
      <audio class="fh-voicebot-audio" autoplay playsinline></audio>
      <button class="fh-voicebot-end" type="button" disabled>End call</button>
    </div>`;
  const style = document.createElement("style");
  style.textContent = `
    .fh-voicebot{position:fixed;right:18px;bottom:18px;z-index:2147483000;font-family:system-ui,-apple-system,Segoe UI,sans-serif;color:#24292f}
    .fh-voicebot-inline{position:static;display:inline-block}
    .fh-voicebot-button,.fh-voicebot-end{font:inherit;border:0;border-radius:999px;padding:12px 16px;cursor:pointer;background:var(--fh-primary,#0969da);color:#fff;box-shadow:0 8px 24px rgba(31,35,40,.18)}
    .fh-voicebot-button:disabled,.fh-voicebot-end:disabled{opacity:.55;cursor:not-allowed}
    .fh-voicebot-button{display:flex;align-items:center;gap:9px;font-weight:700}
    .fh-voicebot-dot{width:10px;height:10px;border-radius:50%;background:#fff;opacity:.9}
    .fh-voicebot-panel{display:none;width:min(320px,calc(100vw - 32px));margin-bottom:10px;padding:14px;border:1px solid #d8dee4;border-radius:8px;background:#fff;box-shadow:0 12px 36px rgba(31,35,40,.18)}
    .fh-voicebot.fh-open .fh-voicebot-panel{display:block}
    .fh-voicebot-title{font-weight:700;margin-bottom:4px}
    .fh-voicebot-status{font-size:14px;color:#57606a;margin-bottom:10px}
    .fh-voicebot-audio{width:100%;height:34px;margin-bottom:10px}
    .fh-voicebot-end{padding:9px 13px;border-radius:6px;box-shadow:none}
    @media (max-width:520px){.fh-voicebot{left:12px;right:12px;bottom:12px}.fh-voicebot-panel{width:auto}.fh-voicebot-button{width:100%;justify-content:center}}`;
  document.head.appendChild(style);
  (currentScript && currentScript.parentElement ? currentScript.parentElement : document.body).appendChild(root);

  const button = root.querySelector(".fh-voicebot-button");
  const label = root.querySelector(".fh-voicebot-label");
  const panel = root.querySelector(".fh-voicebot-panel");
  const title = root.querySelector(".fh-voicebot-title");
  const status = root.querySelector(".fh-voicebot-status");
  const endButton = root.querySelector(".fh-voicebot-end");
  const audio = root.querySelector(".fh-voicebot-audio");

  bootstrap();
  button.addEventListener("click", () => started ? stopCall("ended") : startCall());
  endButton.addEventListener("click", () => stopCall("ended"));

  async function bootstrap() {
    try {
      const payload = await fetchJson(new URL(`${routePath}/.well-known/flowhunt-voicebot`, baseUrl));
      const widget = payload.widget || {};
      label.textContent = widget.launcher_label || payload.display_name || "Voice call";
      title.textContent = widget.welcome_label || payload.display_name || "Voice call";
      if (widget.theme && widget.theme.primary_color) root.style.setProperty("--fh-primary", widget.theme.primary_color);
      if (widget.theme && widget.theme.placement === "bottom-left") { root.style.left = "18px"; root.style.right = "auto"; }
      if (widget.enabled === false) disable("Unavailable");
      root.dataset.sessionEndpoint = payload.session_endpoint || "/webrtc/sessions";
      root.dataset.iceServers = JSON.stringify(payload.ice_servers || []);
      emitMetric("widget_loaded");
    } catch (error) {
      disable("Voicebot unavailable");
      emitMetric("widget_bootstrap_failed", {message: String(error && error.message || error)});
    }
  }

  async function startCall() {
    root.classList.add("fh-open");
    button.disabled = true;
    setStatus("Requesting microphone");
    emitMetric("start_attempt");
    try {
      localStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: {ideal: 1},
          sampleRate: {ideal: 48000},
          sampleSize: {ideal: 16},
          echoCancellation: {ideal: true},
          noiseSuppression: {ideal: true},
          autoGainControl: {ideal: true},
          latency: {ideal: 0.01},
          suppressLocalAudioPlayback: {ideal: true},
        },
        video: false
      });
    } catch (error) {
      button.disabled = false;
      setStatus("Microphone permission denied");
      emitMetric("permission_denied");
      return;
    }
    try {
      pc = new RTCPeerConnection({iceServers: JSON.parse(root.dataset.iceServers || "[]").map(url => typeof url === "string" ? {urls: url} : url)});
      localStream.getTracks().forEach(track => pc.addTrack(track, localStream));
      pc.ontrack = event => { audio.srcObject = event.streams[0]; };
      pc.onconnectionstatechange = () => {
        setStatus(pc.connectionState);
        if (["failed", "closed", "disconnected"].includes(pc.connectionState)) emitMetric(`connection_${pc.connectionState}`);
      };
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      await waitForIceGathering(pc);
      const response = await fetchJson(new URL(root.dataset.sessionEndpoint || "/webrtc/sessions", baseUrl), {
        method: "POST",
        headers: {"content-type": "application/json"},
        body: JSON.stringify({sdp: pc.localDescription.sdp, type: pc.localDescription.type, metadata})
      });
      sessionId = response.session_id;
      await pc.setRemoteDescription(response.answer);
      started = true;
      endButton.disabled = false;
      button.disabled = false;
      label.textContent = "End call";
      setStatus("Connected");
      emitMetric("session_created", {session_id: sessionId});
    } catch (error) {
      setStatus("Connection failed");
      emitMetric("connection_failed", {message: String(error && error.message || error)});
      stopCall("failed");
    }
  }

  async function stopCall(reason) {
    if (sessionId) {
      fetch(new URL(`/webrtc/sessions/${encodeURIComponent(sessionId)}`, baseUrl), {method: "DELETE"}).catch(() => {});
    }
    if (pc) pc.close();
    if (localStream) localStream.getTracks().forEach(track => track.stop());
    pc = null; localStream = null; sessionId = null; started = false;
    endButton.disabled = true; button.disabled = false; label.textContent = "Voice call";
    setStatus(reason === "failed" ? "Failed" : "Ended");
    emitMetric(reason === "failed" ? "failed" : "ended");
  }

  function setStatus(text) { status.textContent = text; }
  function disable(text) { button.disabled = true; setStatus(text); }
  function emitMetric(type, data = {}) { window.dispatchEvent(new CustomEvent("flowhunt-voicebot-widget", {detail: {type, data}})); }
  function safeJson(text) { try { return text ? JSON.parse(text) : {}; } catch (_) { return {}; } }
  async function fetchJson(url, options) {
    const response = await fetch(url, options);
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    return response.json();
  }
  function waitForIceGathering(peer) {
    if (peer.iceGatheringState === "complete") return Promise.resolve();
    return new Promise(resolve => {
      const done = () => {
        if (peer.iceGatheringState === "complete") {
          peer.removeEventListener("icegatheringstatechange", done);
          resolve();
        }
      };
      peer.addEventListener("icegatheringstatechange", done);
      setTimeout(resolve, 2500);
    });
  }
})();"""


WIDGET_PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>FlowHunt Voicebot</title>
</head>
<body>
  <script src="/widget.js" data-inline="true" async></script>
</body>
</html>"""


WEBRTC_TEST_PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Voicebot WebRTC Test</title>
  <style>
    :root { color-scheme: light; --border: #d8dee4; --muted: #57606a; --header: #f6f8fa; --row: #ffffff; --detail: #fbfcfd; --accent: #0969da; --key: #8250df; --value: #1a7f37; }
    body { font-family: system-ui, sans-serif; margin: 1.25rem; line-height: 1.45; color: #24292f; background: #fff; }
    button { font: inherit; margin-right: .5rem; padding: .45rem .75rem; border: 1px solid var(--border); border-radius: 6px; background: #fff; cursor: pointer; }
    button:not(:disabled):hover { border-color: var(--accent); }
    button:disabled { color: #8c959f; cursor: not-allowed; background: #f6f8fa; }
    .call-controls { display: flex; align-items: center; gap: .75rem; margin: 1rem 0; width: 100%; }
    .button-group { display: flex; align-items: center; gap: .5rem; flex: 0 0 auto; }
    audio { display: block; width: 100%; min-width: 16rem; margin: 0; flex: 1 1 auto; }
    .recording-panel { display: none; align-items: center; gap: .75rem; margin: .75rem 0 1rem; padding: .75rem; border: 1px solid var(--border); border-radius: 8px; background: var(--detail); }
    .recording-panel.visible { display: flex; }
    .recording-panel h2 { flex: 0 0 auto; margin: 0; font-size: .875rem; color: var(--muted); }
    .recording-panel audio { min-width: 14rem; }
    .logs { margin-top: 1rem; width: 100%; }
    .test-tabs { display: flex; gap: .35rem; margin: 0 0 .85rem; border-bottom: 1px solid var(--border); overflow: auto; }
    .test-tabs button { border-bottom: 0; border-radius: 6px 6px 0 0; white-space: nowrap; }
    .test-tabs button.active { background: #ddf4ff; border-color: #c9d7e8; color: #0550ae; font-weight: 700; }
    .test-tab-panel { display: none; }
    .test-tab-panel.active { display: block; }
    .log-panel h2 { font-size: 1rem; margin: 0 0 .5rem; }
    .table-filter { margin: 0 0 .5rem; }
    .table-filter input { width: 100%; padding: .45rem .55rem; border: 1px solid var(--border); border-radius: 6px; font: inherit; }
    .table-wrap { border: 1px solid var(--border); border-radius: 8px; height: 30rem; overflow: auto; background: #fff; }
    table { width: 100%; border-collapse: collapse; table-layout: fixed; font-size: .8125rem; }
    thead th { position: sticky; top: 0; z-index: 1; background: var(--header); color: var(--muted); font-weight: 600; text-align: left; border-bottom: 1px solid var(--border); }
    th, td { padding: .45rem .55rem; vertical-align: top; border-bottom: 1px solid #eaeef2; }
    tbody tr.summary-row { background: var(--row); }
    tbody tr.detail-row { background: var(--detail); }
    .time-col { width: 6.75rem; color: var(--muted); font-variant-numeric: tabular-nums; white-space: nowrap; }
    .id-col { width: 4.25rem; color: var(--muted); font-variant-numeric: tabular-nums; white-space: nowrap; }
    .type-col { width: 13rem; }
    .message-cell, .summary-cell, .summary-detail-cell, .json-cell { overflow-wrap: anywhere; word-break: break-word; }
    .event-type, .client-type { display: inline-block; max-width: 100%; padding: .1rem .4rem; border: 1px solid #c9d7e8; border-radius: 999px; color: #0550ae; background: #ddf4ff; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .client-type.error { color: #cf222e; background: #ffebe9; border-color: #ffcecb; }
    .client-type.state { color: #8250df; background: #fbefff; border-color: #ecd8ff; }
    .client-type.audio { color: #1a7f37; background: #dafbe1; border-color: #aceebb; }
    .client-type.caller { color: #1a7f37; background: #dafbe1; border-color: #aceebb; }
    .client-type.voicebot { color: #8250df; background: #fbefff; border-color: #ecd8ff; }
    .summary-cell { color: var(--muted); }
    .summary-detail-cell { padding: .55rem .75rem; color: #24292f; font-size: .8125rem; line-height: 1.5; white-space: pre-wrap; }
    .json-cell { padding: .65rem .75rem .8rem; }
    .json-view { margin: 0; color: #24292f; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: .75rem; line-height: 1.55; white-space: pre-wrap; }
    .json-key { color: var(--key); font-weight: 600; }
    .json-value { color: var(--value); font-weight: 700; }
    .json-string { color: #0a3069; }
    .json-null { color: #6e7781; font-weight: 700; }
    .empty-cell { color: #8c959f; font-family: inherit; }
    @media (max-width: 800px) { .call-controls { align-items: stretch; flex-direction: column; } .button-group { width: 100%; } .button-group button { flex: 1; } audio { min-width: 0; } }
  </style>
</head>
<body>
  <h1>Voicebot WebRTC Test</h1>
  <p>Click Start, allow microphone access, then speak. The bot audio is played by the browser.</p>
  <div class="call-controls">
    <div class="button-group">
      <button id="start">Start call</button>
      <button id="stop" disabled>Stop call</button>
    </div>
    <audio id="remote" autoplay playsinline controls></audio>
  </div>
  <div id="recording-panel" class="recording-panel">
    <h2>Call recording</h2>
    <audio id="recording" controls></audio>
  </div>
  <div class="logs">
    <div class="test-tabs" role="tablist" aria-label="Voicebot WebRTC test logs">
      <button type="button" class="active" data-test-tab="client">Client Log</button>
      <button type="button" data-test-tab="events">Voicebot Events</button>
      <button type="button" data-test-tab="transcript">Call Transcript</button>
      <button type="button" data-test-tab="chat">Widget Chat Preview</button>
      <button type="button" data-test-tab="subagents">Subagent Communication</button>
    </div>
    <section id="test-tab-client" class="log-panel test-tab-panel active">
      <h2>Client Log</h2>
      <div class="table-filter"><input data-table-filter="log" placeholder="Filter client log"></div>
      <div class="table-wrap">
        <table aria-label="Client log">
          <thead>
            <tr><th class="time-col">Time</th><th class="type-col">Type</th></tr>
          </thead>
          <tbody id="log"></tbody>
        </table>
      </div>
    </section>
    <section id="test-tab-events" class="log-panel test-tab-panel">
      <h2>Voicebot Events</h2>
      <div class="table-filter"><input data-table-filter="event-log" placeholder="Filter voicebot events"></div>
      <div class="table-wrap">
        <table aria-label="Voicebot events">
          <thead>
            <tr><th class="time-col">Time</th><th class="id-col">ID</th><th class="type-col">Type</th></tr>
          </thead>
          <tbody id="event-log"></tbody>
        </table>
      </div>
    </section>
    <section id="test-tab-transcript" class="log-panel test-tab-panel">
      <h2>Call Transcript</h2>
      <div class="table-filter"><input data-table-filter="transcript-log" placeholder="Filter transcript"></div>
      <div class="table-wrap">
        <table aria-label="Call transcript">
          <thead>
            <tr><th class="time-col">Time</th><th class="type-col">Speaker</th></tr>
          </thead>
          <tbody id="transcript-log"></tbody>
        </table>
      </div>
    </section>
    <section id="test-tab-subagents" class="log-panel test-tab-panel">
      <h2>Subagent Communication</h2>
      <div class="table-filter"><input data-table-filter="subagent-log" placeholder="Filter subagent communication"></div>
      <div class="table-wrap">
        <table aria-label="Subagent communication">
          <thead>
            <tr><th class="time-col">Time</th><th class="id-col">ID</th><th class="type-col">Type</th></tr>
          </thead>
          <tbody id="subagent-log"></tbody>
        </table>
      </div>
    </section>
    <section id="test-tab-chat" class="log-panel test-tab-panel">
      <h2>Widget Chat Preview</h2>
      <form id="chat-compose" class="chat-compose">
        <input id="chat-input" placeholder="Write a visitor message" autocomplete="off">
        <button id="chat-send" type="submit" disabled>Send</button>
      </form>
      <div class="table-filter"><input data-table-filter="chat-log" placeholder="Filter widget chat preview"></div>
      <div class="table-wrap">
        <table aria-label="Widget chat preview">
          <thead>
            <tr><th class="time-col">Time</th><th class="type-col">Sender</th><th>Message</th></tr>
          </thead>
          <tbody id="chat-log"></tbody>
        </table>
      </div>
    </section>
  </div>
  <script>
    const startButton = document.getElementById("start");
    const stopButton = document.getElementById("stop");
    const remoteAudio = document.getElementById("remote");
    const recordingPanel = document.getElementById("recording-panel");
    const recordingAudio = document.getElementById("recording");
    const logNode = document.getElementById("log");
    const eventLogNode = document.getElementById("event-log");
    const transcriptLogNode = document.getElementById("transcript-log");
    const subagentLogNode = document.getElementById("subagent-log");
    const chatLogNode = document.getElementById("chat-log");
    const chatCompose = document.getElementById("chat-compose");
    const chatInput = document.getElementById("chat-input");
    const chatSend = document.getElementById("chat-send");
    let pc = null;
    let sessionId = null;
    let callId = null;
    let lastFinishedCallId = null;
    let localStream = null;
    let eventSocket = null;
    let eventPollTimer = null;
    let seenEventIds = new Set();
    let testTarget = {client: "browser-test"};

    document.querySelectorAll("[data-table-filter]").forEach((input) => {
      input.addEventListener("input", () => applyTableFilter(input.dataset.tableFilter));
    });
    document.querySelectorAll("[data-test-tab]").forEach((button) => {
      button.addEventListener("click", () => showTestTab(button.dataset.testTab));
    });
    chatCompose.addEventListener("submit", async (event) => {
      event.preventDefault();
      await submitChatMessage();
    });

    window.addEventListener("message", (message) => {
      const data = message.data || {};
      if (data.type !== "voicebot-test-target") return;
      testTarget = {
        client: "browser-test",
        workspace_id: data.workspace_id || "",
        voicebot_id: data.voicebot_id || ""
      };
      log(`target=${JSON.stringify(testTarget)}`);
    });

    function showTestTab(name) {
      const selected = ["client", "events", "transcript", "chat", "subagents"].includes(name) ? name : "client";
      document.querySelectorAll("[data-test-tab]").forEach((button) => {
        button.classList.toggle("active", button.dataset.testTab === selected);
      });
      document.querySelectorAll(".test-tab-panel").forEach((panel) => panel.classList.remove("active"));
      document.getElementById(`test-tab-${selected}`).classList.add("active");
      if (selected === "chat") {
        backfillVoicebotEvents(callId || lastFinishedCallId, {replay: true});
      }
    }

    function formatTime(timestamp) {
      const date = timestamp instanceof Date ? timestamp : new Date(timestamp);
      if (Number.isNaN(date.getTime())) return "";
      const time = new Intl.DateTimeFormat(undefined, {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        fractionalSecondDigits: 3,
        hour12: false
      }).format(date);
      return time.replace(/^24:/, "00:");
    }

    function fullTimestamp(timestamp) {
      const date = timestamp instanceof Date ? timestamp : new Date(timestamp);
      if (Number.isNaN(date.getTime())) return String(timestamp || "");
      return date.toISOString();
    }

    function appendCell(row, text, className = "", title = "") {
      const cell = document.createElement("td");
      if (className) cell.className = className;
      cell.textContent = text;
      if (title) cell.title = title;
      row.appendChild(cell);
      return cell;
    }

    function trimRows(tableBody, maxRows = 300) {
      while (tableBody.rows.length > maxRows * 3) {
        tableBody.deleteRow(0);
      }
    }

    function scrollTableToBottom(tableBody) {
      const wrapper = tableBody.closest(".table-wrap");
      if (wrapper) wrapper.scrollTop = wrapper.scrollHeight;
    }

    function applyTableFilter(tbodyId) {
      const input = document.querySelector(`[data-table-filter="${tbodyId}"]`);
      const tbody = document.getElementById(tbodyId);
      if (!input || !tbody) return;
      const query = normalizeFilterText(input.value);
      for (let index = 0; index < tbody.rows.length; index += 1) {
        const row = tbody.rows[index];
        if (!row.classList.contains("summary-row")) {
          row.hidden = Boolean(query) && !normalizeFilterText(row.textContent || "").includes(query);
          continue;
        }
        const details = [];
        let next = row.nextElementSibling;
        while (next && next.classList.contains("detail-row")) {
          details.push(next);
          next = next.nextElementSibling;
        }
        const combined = normalizeFilterText([row, ...details].map((item) => item.textContent || "").join(" "));
        const match = !query || combined.includes(query);
        row.hidden = !match;
        for (const detail of details) detail.hidden = !match;
      }
    }

    function normalizeFilterText(value) {
      return String(value || "").toLowerCase().replace(/\\s+/g, " ").trim();
    }

    function log(message) {
      if (shouldSuppressClientLogMessage(message)) return;
      const now = new Date();
      appendClientLogRows(now, message);
      applyTableFilter("log");
      trimRows(logNode);
      scrollTableToBottom(logNode);
    }

    function shouldSuppressClientLogMessage(message) {
      return String(message || "").includes("browser-test");
    }

    function appendClientLogRows(timestamp, message) {
      const parsed = parseClientLogMessage(message);
      const row = logNode.insertRow();
      row.className = "summary-row";
      appendCell(row, formatTime(timestamp), "time-col", fullTimestamp(timestamp));
      const typeCell = appendCell(row, "", "type-col");
      const type = document.createElement("span");
      type.className = `client-type ${parsed.kind}`;
      type.textContent = parsed.label;
      type.title = parsed.label;
      typeCell.appendChild(type);
      if (shouldRenderClientSummary(parsed)) {
        appendDetailTextRow(logNode, parsed.summary, 2, "summary-detail-cell");
      }
      if (parsed.detail !== null) {
        const detailRow = logNode.insertRow();
        detailRow.className = "detail-row";
        const detailCell = document.createElement("td");
        detailCell.className = "json-cell";
        detailCell.colSpan = 2;
        detailCell.appendChild(renderJson(parsed.detail));
        detailRow.appendChild(detailCell);
      }
    }

    function parseClientLogMessage(message) {
      if (message.startsWith("local audio settings=")) {
        const raw = message.slice("local audio settings=".length);
        try {
          return {kind: "audio", label: "audio", summary: "Local microphone settings", detail: JSON.parse(raw)};
        } catch {
          return {kind: "audio", label: "audio", summary: message, detail: null};
        }
      }
      if (message.startsWith("connectionState=")) {
        return {kind: "state", label: "state", summary: message.slice("connectionState=".length), detail: null};
      }
      if (message.startsWith("error:") || message.includes("failed")) {
        return {kind: "error", label: "error", summary: message, detail: null};
      }
      if (message.startsWith("received remote")) {
        return {kind: "audio", label: "media", summary: message, detail: null};
      }
      return {kind: "", label: "client", summary: message, detail: null};
    }

    function shouldRenderClientSummary(parsed) {
      if (!parsed.summary) return false;
      if (parsed.detail === null) return true;
      return !jsonContainsStringValue(parsed.detail, parsed.summary);
    }

    function jsonContainsStringValue(value, expected) {
      const normalizedExpected = normalizeSummaryText(expected);
      if (!normalizedExpected) return false;
      if (typeof value === "string") {
        return normalizeSummaryText(value) === normalizedExpected;
      }
      if (Array.isArray(value)) {
        return value.some((item) => jsonContainsStringValue(item, expected));
      }
      if (value && typeof value === "object") {
        return Object.values(value).some((item) => jsonContainsStringValue(item, expected));
      }
      return false;
    }

    function normalizeSummaryText(value) {
      return String(value ?? "").replace(/\\s+/g, " ").trim();
    }

    function logVoicebotEvent(event) {
      const eventKey = event.id ?? `${event.call_id || ""}:${event.type || ""}:${event.timestamp || ""}`;
      if (seenEventIds.has(eventKey)) return;
      seenEventIds.add(eventKey);
      if (isSubagentEvent(event)) logSubagentEvent(event);
      if (isTranscriptEvent(event)) logTranscriptEvent(event);
      if (isChatPreviewEvent(event)) logChatPreviewEvent(event);
      appendEventRows(eventLogNode, event);
      applyTableFilter("event-log");
      trimRows(eventLogNode);
      scrollTableToBottom(eventLogNode);
    }

    function isTranscriptEvent(event) {
      const kind = String(event.data?.response_kind || "");
      if (event.type === "user_transcript_partial" && event.data?.text) return true;
      if (event.type === "user_transcript" && event.data?.text) return true;
      if (event.type === "agent_response_received" && event.data?.text) {
        return kind !== "stream_chunk";
      }
      return false;
    }

    function logTranscriptEvent(event) {
      const isCaller = event.type === "user_transcript" || event.type === "user_transcript_partial";
      const isPartial = event.type === "user_transcript_partial" || event.data?.partial === true;
      const speaker = isCaller ? `Caller${isPartial ? " partial" : ""}` : "Voicebot";
      const kind = isCaller ? "caller" : "voicebot";
      appendTranscriptRows(transcriptLogNode, event.timestamp, speaker, kind, event.data?.text || "");
      applyTableFilter("transcript-log");
      trimRows(transcriptLogNode);
      scrollTableToBottom(transcriptLogNode);
    }

    function isChatPreviewEvent(event) {
      const kind = String(event.data?.response_kind || "");
      if (event.type === "user_speech_started") return true;
      if (event.type === "user_transcript_partial" && event.data?.text) return true;
      if (event.type === "user_transcript" && event.data?.text) return true;
      if (event.type === "stt_no_text") return true;
      if (event.type === "stt_failed") return true;
      if (event.type === "agent_response_received" && !["progress_ack", "call_control_ack", "stream_chunk", "stream_finalized"].includes(kind)) {
        return Boolean(chatTextForEvent(event));
      }
      return false;
    }

    function logChatPreviewEvent(event) {
      const isCaller = event.type === "user_speech_started" || event.type === "user_transcript" || event.type === "user_transcript_partial" || event.type === "stt_no_text" || event.type === "stt_failed";
      const sender = isCaller ? "Visitor" : "Voicebot chat";
      const kind = isCaller ? "caller" : "voicebot";
      const text = isCaller ? chatVisitorTextForEvent(event) : chatTextForEvent(event);
      appendChatPreviewRow(chatLogNode, event.timestamp, sender, kind, text, !isCaller);
      const blocks = !isCaller && Array.isArray(event.data?.chat?.blocks) ? event.data.chat.blocks : [];
      if (blocks.length) appendJsonDetailRow(chatLogNode, blocks, 3);
      applyTableFilter("chat-log");
      trimRows(chatLogNode);
      scrollTableToBottom(chatLogNode);
    }

    function chatVisitorTextForEvent(event) {
      if (event.type === "user_speech_started") return "Listening...";
      if (event.type === "stt_no_text") return "No speech was recognized.";
      if (event.type === "stt_failed") return `Voice transcription failed. ${event.data?.error || "unknown error"}`;
      return String(event.data?.text || "");
    }

    function chatTextForEvent(event) {
      const chat = event.data?.chat;
      if (chat && typeof chat === "object" && typeof chat.text === "string" && chat.text.trim()) {
        return chat.text;
      }
      return String(event.data?.text || "");
    }

    function appendChatPreviewRow(tableBody, timestamp, speaker, kind, text, markdown = false) {
      const row = tableBody.insertRow();
      row.className = "summary-row";
      appendCell(row, formatTime(timestamp), "time-col", fullTimestamp(timestamp));
      const typeCell = appendCell(row, "", "type-col");
      const type = document.createElement("span");
      type.className = `client-type ${kind}`;
      type.textContent = speaker;
      type.title = speaker;
      typeCell.appendChild(type);
      const textCell = document.createElement("td");
      textCell.className = "summary-detail-cell";
      if (markdown) textCell.innerHTML = renderMarkdown(text);
      else textCell.textContent = text;
      row.appendChild(textCell);
    }

    function renderMarkdown(text) {
      const lines = String(text || "").split(/\\r?\\n/);
      const html = [];
      let listOpen = false;
      let paragraph = [];
      const flushParagraph = () => {
        if (!paragraph.length) return;
        html.push(`<p>${renderInlineMarkdown(paragraph.join(" "))}</p>`);
        paragraph = [];
      };
      const closeList = () => {
        if (listOpen) html.push("</ul>");
        listOpen = false;
      };
      for (const rawLine of lines) {
        const line = rawLine.trim();
        if (!line) {
          flushParagraph();
          closeList();
          continue;
        }
        if (line.startsWith("### ")) {
          flushParagraph();
          closeList();
          html.push(`<h3>${renderInlineMarkdown(line.slice(4))}</h3>`);
        } else if (line.startsWith("## ")) {
          flushParagraph();
          closeList();
          html.push(`<h2>${renderInlineMarkdown(line.slice(3))}</h2>`);
        } else if (/^[-*]\\s+/.test(line)) {
          flushParagraph();
          if (!listOpen) {
            html.push("<ul>");
            listOpen = true;
          }
          html.push(`<li>${renderInlineMarkdown(line.replace(/^[-*]\\s+/, ""))}</li>`);
        } else {
          paragraph.push(line);
        }
      }
      flushParagraph();
      closeList();
      return html.join("");
    }

    function renderInlineMarkdown(text) {
      let html = escapeHtml(text);
      html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
      html = html.replace(/\\[([^\\]]+)\\]\\((https?:\\/\\/[^)\\s]+)\\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
      html = html.replace(/(https?:\\/\\/[^\\s<]+)/g, '<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>');
      html = html.replace(/\\*\\*([^*]+)\\*\\*/g, "<strong>$1</strong>");
      html = html.replace(/\\*([^*]+)\\*/g, "<em>$1</em>");
      return html;
    }

    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, (char) => {
        if (char === "&") return "&amp;";
        if (char === "<") return "&lt;";
        if (char === ">") return "&gt;";
        if (char === '"') return "&quot;";
        return "&#39;";
      });
    }

    function appendTranscriptRows(tableBody, timestamp, speaker, kind, text) {
      const row = tableBody.insertRow();
      row.className = "summary-row";
      appendCell(row, formatTime(timestamp), "time-col", fullTimestamp(timestamp));
      const typeCell = appendCell(row, "", "type-col");
      const type = document.createElement("span");
      type.className = `client-type ${kind}`;
      type.textContent = speaker;
      type.title = speaker;
      typeCell.appendChild(type);
      appendDetailTextRow(tableBody, text, 2, "summary-detail-cell");
    }

    function appendJsonDetailRow(tableBody, value, colSpan) {
      const detailRow = tableBody.insertRow();
      detailRow.className = "detail-row";
      const detailCell = document.createElement("td");
      detailCell.className = "json-cell";
      detailCell.colSpan = colSpan;
      detailCell.appendChild(renderJson(value));
      detailRow.appendChild(detailCell);
    }

    function appendEventRows(tableBody, event) {
      const row = tableBody.insertRow();
      row.className = "summary-row";
      appendCell(row, formatTime(event.timestamp), "time-col", fullTimestamp(event.timestamp));
      appendCell(row, event.id ?? "", "id-col");
      const typeCell = appendCell(row, "", "type-col");
      const type = document.createElement("span");
      type.className = "event-type";
      type.textContent = event.type || "";
      type.title = event.type || "";
      typeCell.appendChild(type);

      if (event.data && Object.keys(event.data).length) {
        const detailRow = tableBody.insertRow();
        detailRow.className = "detail-row";
        const detailCell = document.createElement("td");
        detailCell.className = "json-cell";
        detailCell.colSpan = 3;
        detailCell.appendChild(renderJson(event.data));
        detailRow.appendChild(detailCell);
      }
    }

    function appendDetailTextRow(tableBody, text, colSpan, className) {
      const detailRow = tableBody.insertRow();
      detailRow.className = "detail-row";
      const detailCell = document.createElement("td");
      detailCell.className = className;
      detailCell.colSpan = colSpan;
      detailCell.textContent = text;
      detailRow.appendChild(detailCell);
    }

    function renderJson(value, indent = 0) {
      const pre = document.createElement("pre");
      pre.className = "json-view";
      appendJsonValue(pre, value, indent);
      return pre;
    }

    function appendJsonValue(parent, value, indent) {
      if (Array.isArray(value)) {
        parent.append("[");
        if (value.length) parent.append("\\n");
        value.forEach((item, index) => {
          parent.append(" ".repeat(indent + 2));
          appendJsonValue(parent, item, indent + 2);
          if (index < value.length - 1) parent.append(",");
          parent.append("\\n");
        });
        if (value.length) parent.append(" ".repeat(indent));
        parent.append("]");
        return;
      }
      if (value && typeof value === "object") {
        const entries = Object.entries(value);
        parent.append("{");
        if (entries.length) parent.append("\\n");
        entries.forEach(([key, item], index) => {
          parent.append(" ".repeat(indent + 2));
          const keyNode = document.createElement("span");
          keyNode.className = "json-key";
          keyNode.textContent = JSON.stringify(key);
          parent.appendChild(keyNode);
          parent.append(": ");
          appendJsonValue(parent, item, indent + 2);
          if (index < entries.length - 1) parent.append(",");
          parent.append("\\n");
        });
        if (entries.length) parent.append(" ".repeat(indent));
        parent.append("}");
        return;
      }
      const valueNode = document.createElement("span");
      if (typeof value === "string") {
        valueNode.className = "json-value json-string";
        valueNode.textContent = JSON.stringify(value);
      } else if (value === null) {
        valueNode.className = "json-null";
        valueNode.textContent = "null";
      } else {
        valueNode.className = "json-value";
        valueNode.textContent = JSON.stringify(value);
      }
      parent.appendChild(valueNode);
    }

    function isSubagentEvent(event) {
      const data = event.data || {};
      const reason = String(data.reason || "");
      const responseKind = String(data.response_kind || "");
      return String(event.type || "").startsWith("subagent_task_")
        || ["flowhunt_flow_invoked", "flowhunt_project_issue_created"].includes(event.type)
        || reason === "colleague_result"
        || reason === "colleague_progress"
        || responseKind === "colleague_result"
        || responseKind === "colleague_progress"
        || Boolean(data.subagent_task_id)
        || Boolean(data.task && data.task.task_id);
    }

    function logSubagentEvent(event) {
      appendEventRows(subagentLogNode, event);
      applyTableFilter("subagent-log");
      trimRows(subagentLogNode);
      scrollTableToBottom(subagentLogNode);
    }

    function latestSeenEventId() {
      const ids = Array.from(seenEventIds).filter((value) => Number.isInteger(value));
      return ids.length ? Math.max(...ids) : 0;
    }

    async function backfillVoicebotEvents(targetCallId = callId, options = {}) {
      if (!targetCallId) return;
      try {
        const after = options.replay ? 0 : latestSeenEventId();
        const response = await fetch(`/events?call_id=${encodeURIComponent(targetCallId)}&after=${encodeURIComponent(after)}&limit=1000`);
        if (!response.ok) throw new Error(await response.text());
        const payload = await response.json();
        for (const event of payload.events || []) {
          logVoicebotEvent(event);
        }
      } catch (error) {
        log(`event backfill failed: ${error}`);
      }
    }

    function startEventPolling() {
      stopEventPolling();
      eventPollTimer = window.setInterval(() => backfillVoicebotEvents(), 1000);
    }

    function stopEventPolling() {
      if (eventPollTimer) window.clearInterval(eventPollTimer);
      eventPollTimer = null;
    }

    function setIdleButtons() {
      startButton.disabled = false;
      stopButton.disabled = true;
      chatSend.disabled = true;
    }

    function setActiveButtons() {
      startButton.disabled = true;
      stopButton.disabled = false;
      chatSend.disabled = false;
    }

    function resetRecordingPlayback() {
      recordingPanel.classList.remove("visible");
      recordingAudio.removeAttribute("src");
      recordingAudio.load();
    }

    startButton.onclick = async () => {
      startButton.disabled = true;
      resetRecordingPlayback();
      try {
        pc = new RTCPeerConnection({iceServers: [{urls: "stun:stun.l.google.com:19302"}]});
        pc.onconnectionstatechange = () => {
          const state = pc?.connectionState;
          if (!state) return;
          log(`connectionState=${state}`);
          if (["closed", "failed", "disconnected"].includes(state)) {
            closeLocalPeer(`connection ${state}`);
          }
        };
        pc.ontrack = (event) => {
          log(`received remote ${event.track.kind} track`);
          remoteAudio.srcObject = event.streams[0] || new MediaStream([event.track]);
        };
        localStream = await navigator.mediaDevices.getUserMedia({
          audio: {
            channelCount: {ideal: 1},
            sampleRate: {ideal: 48000},
            sampleSize: {ideal: 16},
            echoCancellation: {ideal: true},
            noiseSuppression: {ideal: true},
            autoGainControl: {ideal: true},
            latency: {ideal: 0.01},
            suppressLocalAudioPlayback: {ideal: true}
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
            metadata: testTarget
          })
        });
        if (!response.ok) {
          throw new Error(await response.text());
        }
        const payload = await response.json();
        sessionId = payload.session_id;
        callId = payload.call_id;
        lastFinishedCallId = null;
        seenEventIds = new Set();
        eventLogNode.innerHTML = "";
        transcriptLogNode.innerHTML = "";
        subagentLogNode.innerHTML = "";
        chatLogNode.innerHTML = "";
        connectEventSocket();
        startEventPolling();
        await backfillVoicebotEvents();
        await pc.setRemoteDescription(payload.answer);
        setActiveButtons();
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

    async function submitChatMessage() {
      const text = chatInput.value.trim();
      if (!text || !callId) return;
      chatSend.disabled = true;
      try {
        const response = await fetch(`/calls/${encodeURIComponent(callId)}/messages`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({text, metadata: {source: "widget_chat_preview"}})
        });
        if (!response.ok) throw new Error(await response.text());
        chatInput.value = "";
        await backfillVoicebotEvents();
      } catch (error) {
        log(`chat send failed: ${error}`);
      } finally {
        chatSend.disabled = !callId;
      }
    }

    function connectEventSocket() {
      if (eventSocket) eventSocket.close();
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      eventSocket = new WebSocket(`${protocol}//${window.location.host}/ws/events`);
      eventSocket.onopen = () => {
        backfillVoicebotEvents();
      };
      eventSocket.onmessage = (message) => {
        const event = JSON.parse(message.data);
        if (event.call_id !== callId) return;
        logVoicebotEvent(event);
        if (event.type === "call_control_completed" && event.data?.action === "hangup" && event.data?.ok) {
          closeLocalPeer("server hangup completed");
        }
        if (event.type === "call_ended") {
          closeLocalPeer("server call ended");
        }
      };
      eventSocket.onclose = () => {
        eventSocket = null;
      };
    }

    function closeLocalPeer(reason = "") {
      const finishedCallId = callId;
      setIdleButtons();
      stopEventPolling();
      sessionId = null;
      callId = null;
      if (localStream) {
        for (const track of localStream.getTracks()) track.stop();
      }
      localStream = null;
      const peer = pc;
      pc = null;
      if (peer) {
        peer.close();
      }
      remoteAudio.srcObject = null;
      if (eventSocket) {
        eventSocket.close();
        eventSocket = null;
      }
      if (reason) log(reason);
      if (finishedCallId) {
        lastFinishedCallId = finishedCallId;
        backfillVoicebotEvents(finishedCallId, {replay: true});
        loadCallRecording(finishedCallId);
      }
    }

    async function stopCall() {
      stopButton.disabled = true;
      if (sessionId) {
        try {
          await fetch(`/webrtc/sessions/${sessionId}`, {method: "DELETE"});
        } catch (error) {
          log(`delete failed: ${error}`);
        }
      }
      closeLocalPeer();
      log("stopped");
    }

    async function loadCallRecording(finishedCallId) {
      try {
        const response = await fetch(`/calls/${encodeURIComponent(finishedCallId)}/recording`);
        if (response.status === 404) return;
        if (!response.ok) throw new Error(await response.text());
        const payload = await response.json();
        recordingAudio.src = `/calls/${encodeURIComponent(finishedCallId)}/recording.wav`;
        recordingPanel.classList.add("visible");
        log(`call recording=${JSON.stringify(payload.metadata)}`);
      } catch (error) {
        log(`recording load failed: ${error}`);
      }
    }
  </script>
</body>
</html>
"""
