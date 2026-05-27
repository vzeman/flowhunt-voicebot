from __future__ import annotations

import socketserver
import uuid

from .calls import CallRegistry, CallSession
from .config import Settings
from .events import EventStore
from .stt import STTProvider
from .tts import TTSProvider


class ThreadingAudioSocketServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        settings: Settings,
        events: EventStore,
        registry: CallRegistry,
        stt: STTProvider,
        tts: TTSProvider,
    ) -> None:
        super().__init__(server_address, AudioSocketRequestHandler)
        self.settings = settings
        self.events = events
        self.registry = registry
        self.stt = stt
        self.tts = tts


class AudioSocketRequestHandler(socketserver.BaseRequestHandler):
    server: ThreadingAudioSocketServer

    def handle(self) -> None:
        provisional_call_id = str(uuid.uuid4())
        session = CallSession(
            call_id=provisional_call_id,
            sock=self.request,
            settings=self.server.settings,
            event_store=self.server.events,
            stt=self.server.stt,
            tts=self.server.tts,
        )
        session.set_call_id_change_callback(self.server.registry.replace_id)
        self.server.registry.add(session)
        try:
            session.run()
        finally:
            self.server.registry.remove(session.call_id)
