from __future__ import annotations

import threading
from typing import Any, get_args

from .audiosocket_server import ThreadingAudioSocketServer
from .calls import CallRegistry
from .config import Settings
from .events import EventStore
from .processor_registry import ProcessorSpec, processor_specs_from_config
from .stt import STTProvider
from .transports import (
    ASTERISK_AUDIOSOCKET_CAPABILITIES,
    TRANSPORT_ADAPTER_CONTRACTS,
    TRANSPORT_CAPABILITIES,
    TRANSPORT_UNAVAILABLE_REASONS,
    WEBRTC_CAPABILITIES,
    CallControlRequest,
    CallControlResult,
    MediaSessionDescriptor,
    StaticMediaTransport,
    TransportCapabilities,
    TransportHealth,
    TransportKind,
    TransportRegistry,
)
from .tts import TTSProvider
from .webrtc import WebRTCSessionManager
from .workspace_model import VoicebotSessionStore


class AudioSocketServerTransport:
    kind: TransportKind = "asterisk_audiosocket"
    capabilities: TransportCapabilities = ASTERISK_AUDIOSOCKET_CAPABILITIES

    def __init__(
        self,
        settings: Settings,
        events: EventStore,
        registry: CallRegistry,
        stt: STTProvider,
        tts: TTSProvider,
        audio_artifact_store: Any = None,
        subagent_coordinator: Any = None,
        subagent_lifecycle: Any = None,
    ) -> None:
        self.settings = settings
        self.events = events
        self.registry = registry
        self.stt = stt
        self.tts = tts
        self.audio_artifact_store = audio_artifact_store
        self.subagent_coordinator = subagent_coordinator
        self.subagent_lifecycle = subagent_lifecycle
        self.server: ThreadingAudioSocketServer | None = None
        self.thread: threading.Thread | None = None
        self._stt_pipeline_specs = tuple(processor_specs_from_config(settings.stt_pipeline))
        self._tts_pipeline_specs = tuple(processor_specs_from_config(settings.tts_pipeline))

    @property
    def stt_pipeline_specs(self) -> tuple[ProcessorSpec, ...]:
        return self.server.stt_pipeline_specs if self.server is not None else self._stt_pipeline_specs

    @property
    def tts_pipeline_specs(self) -> tuple[ProcessorSpec, ...]:
        return self.server.tts_pipeline_specs if self.server is not None else self._tts_pipeline_specs

    def start(self) -> TransportHealth:
        if self.server is None:
            self.server = ThreadingAudioSocketServer(
                (self.settings.audiosocket_host, self.settings.audiosocket_port),
                self.settings,
                self.events,
                self.registry,
                self.stt,
                self.tts,
                self.audio_artifact_store,
                self.subagent_coordinator,
                self.subagent_lifecycle,
            )
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
        return self.health()

    def create_session(self, call_id: str, metadata: dict[str, Any] | None = None) -> MediaSessionDescriptor:
        return self.describe_session(call_id, metadata)

    def describe_session(self, call_id: str, metadata: dict[str, Any] | None = None) -> MediaSessionDescriptor:
        return StaticMediaTransport(self.kind, self.capabilities).describe_session(call_id, metadata)

    def receive_inbound_media(self, call_id: str, payload: bytes, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        return StaticMediaTransport(self.kind, self.capabilities).receive_inbound_media(call_id, payload, metadata)

    def send_outbound_media(self, call_id: str, payload: bytes, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        return StaticMediaTransport(self.kind, self.capabilities).send_outbound_media(call_id, payload, metadata)

    def execute_call_control(self, request: CallControlRequest) -> CallControlResult:
        return StaticMediaTransport(self.kind, self.capabilities).execute_call_control(request)

    def health(self) -> TransportHealth:
        running = self.thread is not None and self.thread.is_alive()
        return TransportHealth(
            kind=self.kind,
            ok=running,
            status="ready" if running else "stopped",
            active_sessions=len(self.registry.active_call_ids()),
            details={
                "host": self.settings.audiosocket_host,
                "port": self.settings.audiosocket_port,
                "started": running,
            },
        )

    def shutdown(self) -> TransportHealth:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self.thread is not None:
            self.thread.join(timeout=2.0)
            self.thread = None
        return self.health()


class WebRTCManagerTransport:
    kind: TransportKind = "webrtc"
    capabilities: TransportCapabilities = WEBRTC_CAPABILITIES

    def __init__(
        self,
        manager: WebRTCSessionManager,
    ) -> None:
        self.manager = manager
        self._started = False

    def start(self) -> TransportHealth:
        self._started = True
        return self.health()

    def create_session(self, call_id: str, metadata: dict[str, Any] | None = None) -> MediaSessionDescriptor:
        return self.describe_session(call_id, metadata)

    def describe_session(self, call_id: str, metadata: dict[str, Any] | None = None) -> MediaSessionDescriptor:
        return StaticMediaTransport(self.kind, self.capabilities).describe_session(call_id, metadata)

    def receive_inbound_media(self, call_id: str, payload: bytes, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        return StaticMediaTransport(self.kind, self.capabilities).receive_inbound_media(call_id, payload, metadata)

    def send_outbound_media(self, call_id: str, payload: bytes, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        return StaticMediaTransport(self.kind, self.capabilities).send_outbound_media(call_id, payload, metadata)

    def execute_call_control(self, request: CallControlRequest) -> CallControlResult:
        return StaticMediaTransport(self.kind, self.capabilities).execute_call_control(request)

    def health(self) -> TransportHealth:
        available = self.manager.available()
        return TransportHealth(
            kind=self.kind,
            ok=available and self._started,
            status="ready" if available and self._started else "degraded" if self._started else "stopped",
            active_sessions=len(self.manager.snapshots()),
            details={"available": available, "started": self._started},
        )

    def shutdown(self) -> TransportHealth:
        self._started = False
        return self.health()


def build_runtime_transport_registry(
    settings: Settings,
    events: EventStore,
    call_registry: CallRegistry,
    stt: STTProvider,
    tts: TTSProvider,
    voicebot_sessions: VoicebotSessionStore,
    audio_artifacts: Any = None,
    subagent_coordinator: Any = None,
    subagent_lifecycle: Any = None,
) -> TransportRegistry:
    enabled = set(settings.enabled_transports)
    registry = TransportRegistry()
    audiosocket = AudioSocketServerTransport(
        settings,
        events,
        call_registry,
        stt,
        tts,
        audio_artifacts,
        subagent_coordinator,
        subagent_lifecycle,
    )
    webrtc = WebRTCManagerTransport(
        WebRTCSessionManager(
            settings,
            events,
            call_registry,
            stt,
            tts,
            audiosocket.stt_pipeline_specs,
            audiosocket.tts_pipeline_specs,
            voicebot_sessions,
            audio_artifacts,
            subagent_coordinator,
            subagent_lifecycle,
        )
    )
    registry.register(audiosocket, enabled="asterisk_audiosocket" in enabled)
    registry.register(webrtc, enabled="webrtc" in enabled)
    registry.register(StaticMediaTransport("local", TRANSPORT_CAPABILITIES["local"]), enabled="local" in enabled)
    for kind in get_args(TransportKind):
        if kind in {"asterisk_audiosocket", "webrtc", "local"}:
            continue
        registry.register_planned(
            kind,
            TRANSPORT_CAPABILITIES[kind],
            enabled=kind in enabled,
            adapter_contract=TRANSPORT_ADAPTER_CONTRACTS.get(kind),
            unavailable_reason=TRANSPORT_UNAVAILABLE_REASONS.get(kind),
        )
    for kind in enabled:
        registry.get(kind)  # fail startup for planned or unknown transports selected by config
    return registry
