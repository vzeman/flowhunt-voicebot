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
from .runtime_storage import (
    build_agent_task_tracker,
    build_call_state_store,
    build_event_store,
    build_voicebot_session_store,
    build_worker_queue_store,
)
from .sip_trunks import SipTrunkStore
from .subagents import FlowHuntSubagentProvider, JsonSubagentTaskStore, SubagentCoordinator
from .transcripts import TranscriptStore
from .webrtc import WebRTCSessionManager


def main() -> None:
    settings = Settings()
    hub = WebSocketHub()
    transcripts = TranscriptStore(settings.transcript_dir)
    events = build_event_store(settings, transcripts)
    voicebot_sessions = build_voicebot_session_store(settings)
    registry = CallRegistry(build_call_state_store(settings))
    tracker = build_agent_task_tracker(settings)
    worker_queue = build_worker_queue_store(settings)
    sip_trunks = SipTrunkStore(settings.sip_trunk_registry_path, settings.sip_trunk_pjsip_include_path)
    subagents = build_subagent_coordinator(settings, events)
    asterisk = (
        AsteriskAMI(settings.ami_host, settings.ami_port, settings.ami_username, settings.ami_password)
        if settings.ami_password
        else None
    )

    providers = default_provider_registry()
    stt = providers.build_stt(settings)
    tts = providers.build_tts(settings)

    audiosocket_server = ThreadingAudioSocketServer(
        (settings.audiosocket_host, settings.audiosocket_port),
        settings,
        events,
        registry,
        stt,
        tts,
    )
    thread = threading.Thread(target=audiosocket_server.serve_forever, daemon=True)
    thread.start()
    print(f"AudioSocket listening on {settings.audiosocket_host}:{settings.audiosocket_port}")

    webrtc = WebRTCSessionManager(
        settings,
        events,
        registry,
        stt,
        tts,
        audiosocket_server.stt_pipeline_specs,
        audiosocket_server.tts_pipeline_specs,
        voicebot_sessions,
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
        worker_queue=worker_queue,
        voicebot_sessions=voicebot_sessions,
    )
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)

    audiosocket_server.shutdown()
    thread.join(timeout=2.0)


def build_subagent_coordinator(settings: Settings, events: EventStore) -> SubagentCoordinator:
    store = JsonSubagentTaskStore(settings.subagent_task_store_path)
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
    return coordinator


if __name__ == "__main__":
    main()
