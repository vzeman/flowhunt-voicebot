from __future__ import annotations

import asyncio
import threading

import uvicorn

from .api import WebSocketHub, create_app
from .asterisk_control import AsteriskAMI
from .audiosocket_server import ThreadingAudioSocketServer
from .calls import CallRegistry
from .config import Settings
from .events import EventStore
from .flowhunt import FlowHuntClient
from .provider_registry import default_provider_registry
from .processor_registry import processor_specs_from_config
from .runtime_storage import (
    build_agent_task_tracker,
    build_audio_artifact_store,
    build_call_state_store,
    build_event_store,
    build_provider_config_store,
    build_sip_trunk_store,
    build_session_lease_store,
    build_subagent_task_store,
    build_transcript_store,
    build_worker_registry,
    build_voicebot_session_store,
    build_worker_queue_store,
)
from .subagents import FlowHuntSubagentProvider, HttpSubagentProvider, HttpSubagentProviderManifest, SubagentCoordinator
from .transports import TransportKind, TransportRegistry, default_transport_registry
from .webrtc import WebRTCSessionManager
from .workspace_model import VoicebotDefinition, VoicebotStore


def main() -> None:
    settings = Settings()
    hub = WebSocketHub()
    transcripts = build_transcript_store(settings)
    events = build_event_store(settings, transcripts)
    voicebot_sessions = build_voicebot_session_store(settings)
    session_leases = build_session_lease_store(settings)
    registry = CallRegistry(build_call_state_store(settings))
    tracker = build_agent_task_tracker(settings)
    worker_registry = build_worker_registry(settings)
    worker_queue = build_worker_queue_store(settings)
    sip_trunks = build_sip_trunk_store(settings)
    provider_configs = build_provider_config_store(settings)
    voicebots = build_default_voicebot_store(settings)
    audio_artifacts = build_audio_artifact_store(settings)
    subagents = build_subagent_coordinator(settings, events)
    asterisk = (
        AsteriskAMI(settings.ami_host, settings.ami_port, settings.ami_username, settings.ami_password)
        if settings.ami_password
        else None
    )

    providers = default_provider_registry()
    stt = providers.build_stt(settings)
    tts = providers.build_tts(settings)

    transport_registry = build_transport_registry(settings)
    audiosocket_server = None
    thread = None
    if transport_enabled(transport_registry, "asterisk_audiosocket"):
        audiosocket_server = ThreadingAudioSocketServer(
            (settings.audiosocket_host, settings.audiosocket_port),
            settings,
            events,
            registry,
            stt,
            tts,
            audio_artifacts,
        )
        thread = threading.Thread(target=audiosocket_server.serve_forever, daemon=True)
        thread.start()
        print(f"AudioSocket listening on {settings.audiosocket_host}:{settings.audiosocket_port}")
    stt_pipeline_specs = (
        audiosocket_server.stt_pipeline_specs
        if audiosocket_server is not None
        else tuple(processor_specs_from_config(settings.stt_pipeline))
    )
    tts_pipeline_specs = (
        audiosocket_server.tts_pipeline_specs
        if audiosocket_server is not None
        else tuple(processor_specs_from_config(settings.tts_pipeline))
    )

    webrtc = None
    if transport_enabled(transport_registry, "webrtc"):
        webrtc = WebRTCSessionManager(
            settings,
            events,
            registry,
            stt,
            tts,
            stt_pipeline_specs,
            tts_pipeline_specs,
            voicebot_sessions,
            audio_artifacts,
        )
    app = create_app(
        events,
        registry,
        tracker,
        hub,
        transcripts,
        asterisk,
        settings,
        sip_trunks,
        webrtc,
        subagents,
        provider_configs=provider_configs,
        worker_queue=worker_queue,
        worker_registry=worker_registry,
        voicebots=voicebots,
        voicebot_sessions=voicebot_sessions,
        session_leases=session_leases,
        audio_artifacts=audio_artifacts,
    )
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)

    if audiosocket_server is not None:
        audiosocket_server.shutdown()
    if thread is not None:
        thread.join(timeout=2.0)


def build_transport_registry(settings: Settings) -> TransportRegistry:
    enabled = set(settings.enabled_transports)
    registry = default_transport_registry(enabled_kinds=enabled)
    for kind in enabled:
        registry.get(kind)  # fail startup for planned or unknown transports selected by config
    return registry


def transport_enabled(registry: TransportRegistry, kind: TransportKind) -> bool:
    try:
        registry.get(kind)
    except (KeyError, ValueError):
        return False
    return True


def build_subagent_coordinator(settings: Settings, events: EventStore) -> SubagentCoordinator:
    store = build_subagent_task_store(settings)
    coordinator = SubagentCoordinator(store=store, events=events)
    client = FlowHuntClient(
        api_key=settings.flowhunt_api_key,
        workspace_id=settings.flowhunt_workspace_id,
        base_url=settings.flowhunt_base_url,
        timeout=settings.flowhunt_timeout,
    )
    if settings.flowhunt_flow_id:
        coordinator.register(FlowHuntSubagentProvider("flowhunt_flow", client, settings.flowhunt_flow_id))
    if settings.flowhunt_project_id:
        coordinator.register(FlowHuntSubagentProvider("flowhunt_project", client, settings.flowhunt_project_id))
    provider_configs = settings.subagent_providers or settings.http_subagent_providers
    for provider_config in provider_configs:
        manifest = HttpSubagentProviderManifest(
            kind=str(provider_config.get("kind") or provider_config.get("provider") or "http_service"),
            submit_url=str(provider_config.get("submit_url") or ""),
            label=str(provider_config.get("label") or "HTTP subagent service"),
            poll_url=str(provider_config["poll_url"]) if provider_config.get("poll_url") else None,
            cancel_url=str(provider_config["cancel_url"]) if provider_config.get("cancel_url") else None,
            headers={str(key): str(value) for key, value in dict(provider_config.get("headers") or {}).items()},
            timeout_seconds=float(provider_config.get("timeout_seconds") or 10.0),
            required_metadata=tuple(str(item) for item in provider_config.get("required_metadata") or ()),
            result_context=str(provider_config.get("result_context") or "clean"),  # type: ignore[arg-type]
        )
        provider = HttpSubagentProvider(manifest)
        coordinator.register(provider, provider.descriptor)
    return coordinator


def build_default_voicebot_store(settings: Settings) -> VoicebotStore:
    store = VoicebotStore()
    if settings.default_workspace_id and settings.default_voicebot_id:
        store.create(
            VoicebotDefinition(
                settings.default_workspace_id,
                settings.default_voicebot_id,
                display_name=settings.default_voicebot_display_name or settings.default_voicebot_id,
                enabled=True,
                metadata={"source": "local_default_seed"},
            )
        )
    return store


if __name__ == "__main__":
    main()
