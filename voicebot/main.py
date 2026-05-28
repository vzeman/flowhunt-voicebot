from __future__ import annotations

import asyncio
import threading

import uvicorn

from .agent_tasks import AgentTaskTracker
from .api import WebSocketHub, create_app
from .asterisk_control import AsteriskAMI
from .audiosocket_server import ThreadingAudioSocketServer
from .calls import CallRegistry
from .config import Settings
from .events import EventStore
from .provider_registry import default_provider_registry
from .sip_trunks import SipTrunkStore
from .transcripts import TranscriptStore


def main() -> None:
    settings = Settings()
    hub = WebSocketHub()
    transcripts = TranscriptStore(settings.transcript_dir)
    events = EventStore(settings.max_context_events, transcript_store=transcripts)
    registry = CallRegistry()
    tracker = AgentTaskTracker(settings.agent_task_responded_event_retention)
    sip_trunks = SipTrunkStore(settings.sip_trunk_registry_path, settings.sip_trunk_pjsip_include_path)
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

    app = create_app(events, registry, tracker, hub, transcripts, asterisk, settings, sip_trunks)
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)

    audiosocket_server.shutdown()
    thread.join(timeout=2.0)


if __name__ == "__main__":
    main()
