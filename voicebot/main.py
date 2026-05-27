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
from .providers import (
    STT_OPENAI_COMPATIBLE_PROVIDERS,
    SUPPORTED_STT_PROVIDERS,
    SUPPORTED_TTS_PROVIDERS,
    TTS_OPENAI_COMPATIBLE_PROVIDERS,
    normalize_provider,
    unsupported_provider_message,
)
from .stt import OpenAISTTProvider, WhisperSTTProvider
from .transcripts import TranscriptStore
from .tts import OpenAITTSProvider, SupertonicTTSProvider


def build_stt(settings: Settings):
    provider = normalize_provider(settings.stt_provider)
    if provider == "whisper":
        return WhisperSTTProvider(settings)
    if provider in STT_OPENAI_COMPATIBLE_PROVIDERS:
        return OpenAISTTProvider(settings)
    if provider in SUPPORTED_STT_PROVIDERS:
        raise ValueError(
            unsupported_provider_message(
                "STT",
                provider,
                SUPPORTED_STT_PROVIDERS,
                "Use an OpenAI-compatible endpoint with VOICEBOT_STT_PROVIDER=openai-compatible, "
                "VOICEBOT_STT_BASE_URL, VOICEBOT_STT_API_KEY, and VOICEBOT_STT_MODEL until a native adapter is added.",
            )
        )
    raise ValueError(f"Unknown STT provider: {settings.stt_provider}")


def build_tts(settings: Settings):
    provider = normalize_provider(settings.tts_provider)
    if provider == "supertonic":
        return SupertonicTTSProvider(settings.tts_voice, settings.language)
    if provider in TTS_OPENAI_COMPATIBLE_PROVIDERS:
        return OpenAITTSProvider(settings)
    if provider in SUPPORTED_TTS_PROVIDERS:
        raise ValueError(
            unsupported_provider_message(
                "TTS",
                provider,
                SUPPORTED_TTS_PROVIDERS,
                "Use an OpenAI-compatible endpoint with VOICEBOT_TTS_PROVIDER=openai-compatible, "
                "VOICEBOT_TTS_BASE_URL, VOICEBOT_TTS_API_KEY, VOICEBOT_TTS_MODEL, and "
                "VOICEBOT_OPENAI_TTS_VOICE until a native adapter is added.",
            )
        )
    raise ValueError(f"Unknown TTS provider: {settings.tts_provider}")


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
