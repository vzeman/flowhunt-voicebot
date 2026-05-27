from __future__ import annotations

import asyncio
import threading

import uvicorn

from .api import AgentTaskTracker, WebSocketHub, create_app
from .asterisk_control import AsteriskAMI
from .audiosocket_server import ThreadingAudioSocketServer
from .calls import CallRegistry
from .config import Settings
from .events import EventStore
from .stt import WhisperSTTProvider
from .transcripts import TranscriptStore
from .tts import SupertonicTTSProvider


def build_stt(settings: Settings):
    if settings.stt_provider != "whisper":
        raise ValueError(f"Unsupported STT provider: {settings.stt_provider}")
    return WhisperSTTProvider(settings)


def build_tts(settings: Settings):
    if settings.tts_provider != "supertonic":
        raise ValueError(f"Unsupported TTS provider: {settings.tts_provider}")
    return SupertonicTTSProvider(settings.tts_voice, settings.language)


def main() -> None:
    settings = Settings()
    hub = WebSocketHub()
    transcripts = TranscriptStore(settings.transcript_dir)
    events = EventStore(settings.max_context_events, transcript_store=transcripts)
    registry = CallRegistry()
    tracker = AgentTaskTracker()
    asterisk = (
        AsteriskAMI(settings.ami_host, settings.ami_port, settings.ami_username, settings.ami_password)
        if settings.ami_password
        else None
    )

    stt = build_stt(settings)
    tts = build_tts(settings)

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

    app = create_app(events, registry, tracker, hub, transcripts, asterisk)
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)

    audiosocket_server.shutdown()
    thread.join(timeout=2.0)


if __name__ == "__main__":
    main()
