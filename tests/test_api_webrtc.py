from __future__ import annotations

import tempfile
import unittest

import numpy as np
from fastapi.testclient import TestClient

from voicebot.audio import STT_SAMPLE_RATE
from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.calls import CallRegistry
from voicebot.config import Settings
from voicebot.events import EventStore
from voicebot.pipeline_contract import PIPELINE_CONTRACT_VERSION
from voicebot.call_recording import recording_artifact_id
from voicebot.storage.artifacts import FilesystemArtifactStore
from voicebot.transcripts import TranscriptStore
from voicebot.webrtc import WebRTCCallSession, WebRTCSessionManager, audio_frame_to_call_audio
from voicebot.workspace_model import (
    ChannelResolver,
    PublicVoicebotRoute,
    PublicVoicebotRouteStore,
    VoicebotChannelBinding,
    VoicebotDefinition,
    VoicebotSessionStore,
    VoicebotStore,
)


class FakeWebRTCManager:
    def __init__(self) -> None:
        self.created = []
        self.closed = []
        self.session_snapshots = [{"session_id": "session-1", "call_id": "webrtc-session-1", "transport": "webrtc"}]

    def snapshots(self):
        return self.session_snapshots

    async def create_session(self, sdp: str, offer_type: str, metadata: dict):
        self.created.append((sdp, offer_type, metadata))
        return {
            "session_id": "session-1",
            "call_id": "webrtc-session-1",
            "answer": {"sdp": "answer-sdp", "type": "answer"},
        }

    async def close_session(self, session_id: str) -> bool:
        self.closed.append(session_id)
        return session_id == "session-1"


class FakeAudioFrame:
    sample_rate = 48000

    def to_ndarray(self):
        return np.ones((1, 480), dtype=np.float32) * 0.25


class FakePackedStereoAudioFrame:
    sample_rate = 48000

    def to_ndarray(self):
        return np.ones((480, 2), dtype=np.int16) * 8192


class FakeChannelList:
    channels = [object(), object()]


class FakeSinglePlanePackedStereoAudioFrame:
    sample_rate = 48000
    samples = 480
    layout = FakeChannelList()

    def to_ndarray(self):
        return np.ones((1, 960), dtype=np.int16) * 8192


class FakeSTT:
    def transcribe(self, audio, sample_rate=8000):
        raise AssertionError("STT should not run in this test")


class FakeTTS:
    def synthesize(self, text: str):
        raise AssertionError("TTS should not run in this test")


class ApiWebRTCTests(unittest.TestCase):
    def build_client(self, webrtc=None) -> tuple[TestClient, FakeWebRTCManager | None]:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        manager = webrtc
        app = create_app(
            EventStore(max_context_events=20),
            CallRegistry(),
            AgentTaskTracker(),
            WebSocketHub(),
            TranscriptStore(directory.name),
            None,
            webrtc=manager,
        )
        return TestClient(app), manager

    def test_webrtc_sessions_requires_configured_transport(self) -> None:
        client, _manager = self.build_client()

        response = client.get("/webrtc/sessions")

        self.assertEqual(response.status_code, 503)

    def test_create_webrtc_session_returns_answer(self) -> None:
        client, manager = self.build_client(FakeWebRTCManager())

        response = client.post(
            "/webrtc/sessions",
            json={"sdp": "offer-sdp", "type": "offer", "metadata": {"tenant_id": "tenant-1"}},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["answer"], {"sdp": "answer-sdp", "type": "answer"})
        self.assertEqual(manager.created, [("offer-sdp", "offer", {"tenant_id": "tenant-1"})])

    def test_create_webrtc_session_resolves_public_route_from_forwarded_host_and_prefix(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        voicebots = VoicebotStore()
        voicebots.create(VoicebotDefinition("workspace-1", "voicebot-1", enabled=True))
        channels = ChannelResolver(
            [
                VoicebotChannelBinding(
                    "channel-1",
                    "webrtc_widget",
                    "workspace-1",
                    "voicebot-1",
                    "widget-1",
                )
            ]
        )
        routes = PublicVoicebotRouteStore(
            [
                PublicVoicebotRoute(
                    "route-1",
                    "workspace-1",
                    "voicebot-1",
                    "channel-1",
                    "voice.example.com",
                    "/support",
                    status="active",
                )
            ]
        )
        manager = FakeWebRTCManager()
        app = create_app(
            EventStore(max_context_events=20),
            CallRegistry(),
            AgentTaskTracker(),
            WebSocketHub(),
            TranscriptStore(directory.name),
            None,
            webrtc=manager,
            voicebots=voicebots,
            channels=channels,
            public_routes=routes,
        )
        client = TestClient(app)

        response = client.post(
            "/webrtc/sessions",
            headers={"x-forwarded-host": "voice.example.com", "x-forwarded-prefix": "/support"},
            json={"sdp": "offer-sdp", "type": "offer", "metadata": {"client": "browser"}},
        )

        self.assertEqual(response.status_code, 200)
        metadata = manager.created[0][2]
        self.assertEqual(metadata["workspace_id"], "workspace-1")
        self.assertEqual(metadata["voicebot_id"], "voicebot-1")
        self.assertEqual(metadata["channel_id"], "channel-1")
        self.assertEqual(metadata["public_route_id"], "route-1")
        self.assertTrue(metadata["public_route_resolved"])

    def test_create_webrtc_session_rejects_disabled_public_route_channel(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        voicebots = VoicebotStore()
        voicebots.create(VoicebotDefinition("workspace-1", "voicebot-1", enabled=True))
        channels = ChannelResolver(
            [
                VoicebotChannelBinding(
                    "channel-1",
                    "webrtc_widget",
                    "workspace-1",
                    "voicebot-1",
                    "widget-1",
                    enabled=False,
                )
            ]
        )
        routes = PublicVoicebotRouteStore(
            [
                PublicVoicebotRoute(
                    "route-1",
                    "workspace-1",
                    "voicebot-1",
                    "channel-1",
                    "voice.example.com",
                    "/",
                    status="active",
                )
            ]
        )
        manager = FakeWebRTCManager()
        app = create_app(
            EventStore(max_context_events=20),
            CallRegistry(),
            AgentTaskTracker(),
            WebSocketHub(),
            TranscriptStore(directory.name),
            None,
            webrtc=manager,
            voicebots=voicebots,
            channels=channels,
            public_routes=routes,
        )
        client = TestClient(app)

        response = client.post(
            "/webrtc/sessions",
            headers={"host": "voice.example.com"},
            json={"sdp": "offer-sdp", "type": "offer"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(manager.created, [])

    def test_public_bootstrap_returns_caller_safe_route_metadata(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        voicebots = VoicebotStore()
        voicebots.create(VoicebotDefinition("workspace-1", "voicebot-1", display_name="Support", enabled=True))
        channels = ChannelResolver(
            [VoicebotChannelBinding("channel-1", "webrtc_widget", "workspace-1", "voicebot-1", "widget-1")]
        )
        routes = PublicVoicebotRouteStore(
            [PublicVoicebotRoute("route-1", "workspace-1", "voicebot-1", "channel-1", "voice.example.com", "/", status="active")]
        )
        app = create_app(
            EventStore(max_context_events=20),
            CallRegistry(),
            AgentTaskTracker(),
            WebSocketHub(),
            TranscriptStore(directory.name),
            None,
            webrtc=FakeWebRTCManager(),
            voicebots=voicebots,
            channels=channels,
            public_routes=routes,
        )
        client = TestClient(app)

        response = client.get("/.well-known/flowhunt-voicebot", headers={"host": "voice.example.com"})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["route_id"], "route-1")
        self.assertEqual(payload["display_name"], "Support")
        self.assertEqual(payload["session_endpoint"], "/webrtc/sessions")
        self.assertNotIn("prompt_config", str(payload))
        self.assertNotIn("api_key", str(payload).lower())

    def test_public_bootstrap_rejects_unknown_route(self) -> None:
        client, _manager = self.build_client(FakeWebRTCManager())

        response = client.get("/.well-known/flowhunt-voicebot", headers={"host": "unknown.example.com"})

        self.assertEqual(response.status_code, 404)

    def test_public_webrtc_session_rejects_blocked_origin(self) -> None:
        client, manager = self.public_route_client(allowed_origins=("https://allowed.example",))

        response = client.post(
            "/webrtc/sessions",
            headers={"host": "voice.example.com", "origin": "https://blocked.example"},
            json={"sdp": "offer-sdp", "type": "offer"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "Origin is not allowed for this voicebot route")
        self.assertEqual(manager.created, [])

    def test_public_webrtc_session_rate_limits_by_route(self) -> None:
        client, manager = self.public_route_client(settings=Settings(public_session_rate_limit_per_minute=1))

        first = client.post(
            "/webrtc/sessions",
            headers={"host": "voice.example.com"},
            json={"sdp": "offer-sdp", "type": "offer"},
        )
        second = client.post(
            "/webrtc/sessions",
            headers={"host": "voice.example.com"},
            json={"sdp": "offer-sdp", "type": "offer"},
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.json()["detail"], "public_route_rate_limited")
        self.assertEqual(len(manager.created), 1)

    def test_public_webrtc_session_rejects_capacity_full(self) -> None:
        manager = FakeWebRTCManager()
        manager.session_snapshots = [
            {
                "session_id": "session-existing",
                "call_id": "webrtc-existing",
                "transport": "webrtc",
                "metadata": {"workspace_id": "workspace-1", "voicebot_id": "voicebot-1"},
            }
        ]
        client, _manager = self.public_route_client(
            manager=manager,
            settings=Settings(public_voicebot_max_concurrent_sessions=1),
        )

        response = client.post(
            "/webrtc/sessions",
            headers={"host": "voice.example.com"},
            json={"sdp": "offer-sdp", "type": "offer"},
        )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["detail"], "Voicebot session capacity is full")

    def public_route_client(
        self,
        *,
        allowed_origins: tuple[str, ...] = (),
        settings: Settings | None = None,
        manager: FakeWebRTCManager | None = None,
    ) -> tuple[TestClient, FakeWebRTCManager]:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        voicebots = VoicebotStore()
        voicebots.create(VoicebotDefinition("workspace-1", "voicebot-1", enabled=True))
        channels = ChannelResolver(
            [VoicebotChannelBinding("channel-1", "webrtc_widget", "workspace-1", "voicebot-1", "widget-1")]
        )
        routes = PublicVoicebotRouteStore(
            [
                PublicVoicebotRoute(
                    "route-1",
                    "workspace-1",
                    "voicebot-1",
                    "channel-1",
                    "voice.example.com",
                    "/",
                    status="active",
                    allowed_origins=allowed_origins,
                )
            ]
        )
        active_manager = manager or FakeWebRTCManager()
        app = create_app(
            EventStore(max_context_events=20),
            CallRegistry(),
            AgentTaskTracker(),
            WebSocketHub(),
            TranscriptStore(directory.name),
            None,
            settings=settings,
            webrtc=active_manager,
            voicebots=voicebots,
            channels=channels,
            public_routes=routes,
        )
        return TestClient(app), active_manager

    def test_create_webrtc_session_rejects_non_offer_type(self) -> None:
        client, _manager = self.build_client(FakeWebRTCManager())

        response = client.post("/webrtc/sessions", json={"sdp": "answer-sdp", "type": "answer"})

        self.assertEqual(response.status_code, 400)

    def test_delete_webrtc_session_closes_manager_session(self) -> None:
        client, manager = self.build_client(FakeWebRTCManager())

        response = client.delete("/webrtc/sessions/session-1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"closed": True, "session_id": "session-1"})
        self.assertEqual(manager.closed, ["session-1"])

    def test_delete_webrtc_session_returns_404_for_unknown_session(self) -> None:
        client, _manager = self.build_client(FakeWebRTCManager())

        response = client.delete("/webrtc/sessions/missing")

        self.assertEqual(response.status_code, 404)

    def test_webrtc_test_page_closes_on_backend_hangup_event(self) -> None:
        client, _manager = self.build_client(FakeWebRTCManager())

        response = client.get("/webrtc/test")

        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn("/ws/events", html)
        self.assertIn("/events?limit=160", html)
        self.assertIn('id="event-log"', html)
        self.assertIn('id="subagent-log"', html)
        self.assertIn('id="recording-panel"', html)
        self.assertIn('id="recording"', html)
        self.assertIn("loadCallRecording(finishedCallId)", html)
        self.assertIn('/recording.wav', html)
        self.assertIn('aria-label="Client log"', html)
        self.assertIn('aria-label="Voicebot events"', html)
        self.assertIn('aria-label="Subagent communication"', html)
        self.assertIn("Voicebot Events", html)
        self.assertIn("Subagent Communication", html)
        self.assertNotIn("<th>Summary</th>", html)
        self.assertIn('class="call-controls"', html)
        self.assertIn('class="button-group"', html)
        self.assertIn("appendClientLogRows(now, message)", html)
        self.assertIn("parseClientLogMessage(message)", html)
        self.assertIn('message.startsWith("local audio settings=")', html)
        self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr))", html)
        self.assertIn('body { font-family: system-ui, sans-serif; margin: 1.25rem;', html)
        self.assertIn('detailCell.colSpan = 2', html)
        self.assertIn("formatTime(timestamp)", html)
        self.assertIn("fullTimestamp(timestamp)", html)
        self.assertIn("logVoicebotEvent(event)", html)
        self.assertIn("appendEventRows(eventLogNode, event)", html)
        self.assertIn("detailCell.colSpan = 3", html)
        self.assertIn("renderJson(event.data)", html)
        self.assertIn("appendDetailTextRow(logNode, parsed.summary, 2", html)
        self.assertIn("summary-detail-cell", html)
        self.assertIn("shouldRenderClientSummary(parsed)", html)
        self.assertIn("jsonContainsStringValue(parsed.detail, parsed.summary)", html)
        self.assertIn("normalizeSummaryText(value)", html)
        self.assertIn('className = "json-value"', html)
        self.assertIn('className = "json-key"', html)
        self.assertIn("isSubagentEvent(event)", html)
        self.assertIn("logSubagentEvent(event)", html)
        self.assertIn('startsWith("subagent_task_")', html)
        self.assertIn('reason === "colleague_result"', html)
        self.assertIn("eventLogNode.innerHTML = \"\"", html)
        self.assertIn("subagentLogNode.innerHTML = \"\"", html)
        self.assertIn('event.type === "call_control_completed"', html)
        self.assertIn('event.data?.action === "hangup"', html)
        self.assertIn("setIdleButtons()", html)
        self.assertIn('closeLocalPeer("server hangup completed")', html)
        self.assertIn('closeLocalPeer(`connection ${state}`)', html)

    def test_call_recording_endpoints_return_metadata_and_audio(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifacts = FilesystemArtifactStore(f"{directory}/artifacts")
            artifacts.put(
                recording_artifact_id("call-1"),
                b"RIFF....WAVE",
                {"call_id": "call-1", "segment_count": 1},
            )
            app = create_app(
                EventStore(max_context_events=20),
                CallRegistry(),
                AgentTaskTracker(),
                WebSocketHub(),
                TranscriptStore(directory),
                None,
                audio_artifacts=artifacts,
            )
            client = TestClient(app)

            metadata = client.get("/calls/call-1/recording")
            audio = client.get("/calls/call-1/recording.wav")

        self.assertEqual(metadata.status_code, 200)
        self.assertEqual(metadata.json()["metadata"]["segment_count"], 1)
        self.assertEqual(audio.status_code, 200)
        self.assertEqual(audio.headers["content-type"], "audio/wav")
        self.assertEqual(audio.content, b"RIFF....WAVE")

    def test_webrtc_manager_persists_routed_session_lifecycle(self) -> None:
        events = EventStore(max_context_events=20)
        store = VoicebotSessionStore()
        manager = self._build_real_manager(events, store)
        session = WebRTCCallSession(
            call_id="webrtc-session-1",
            session_id="session-1",
            settings=Settings(),
            event_store=events,
            stt=FakeSTT(),
            tts=FakeTTS(),
            metadata={
                "workspace_id": "workspace-1",
                "voicebot_id": "voicebot-1",
                "channel_id": "widget-1",
            },
        )

        manager._persist_session_started(session)
        manager._persist_session_ended(session)

        persisted = store.get("session-1", workspace_id="workspace-1")
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.status if persisted else None, "ended")
        self.assertEqual(persisted.channel_id if persisted else None, "widget-1")
        self.assertEqual(persisted.external_session_id if persisted else None, "webrtc-session-1")

    def test_webrtc_manager_skips_unrouted_session_persistence(self) -> None:
        events = EventStore(max_context_events=20)
        store = VoicebotSessionStore()
        manager = self._build_real_manager(events, store)
        session = WebRTCCallSession(
            call_id="webrtc-session-1",
            session_id="session-1",
            settings=Settings(),
            event_store=events,
            stt=FakeSTT(),
            tts=FakeTTS(),
        )

        manager._persist_session_started(session)

        self.assertEqual(store.list(), ())

    def _build_real_manager(self, events: EventStore, store: VoicebotSessionStore) -> WebRTCSessionManager:
        return WebRTCSessionManager(
            Settings(),
            events,
            CallRegistry(),
            FakeSTT(),
            FakeTTS(),
            (),
            (),
            store,
        )

    def test_webrtc_audio_is_resampled_to_stt_sample_rate(self) -> None:
        audio = audio_frame_to_call_audio(FakeAudioFrame())

        self.assertEqual(len(audio), 160)
        self.assertEqual(STT_SAMPLE_RATE, 16000)

    def test_webrtc_audio_handles_packed_stereo_frames(self) -> None:
        audio = audio_frame_to_call_audio(FakePackedStereoAudioFrame())

        self.assertEqual(len(audio), 160)
        self.assertAlmostEqual(float(audio.mean()), 0.25, delta=0.02)

    def test_webrtc_audio_handles_single_plane_packed_stereo_frames(self) -> None:
        audio = audio_frame_to_call_audio(FakeSinglePlanePackedStereoAudioFrame())

        self.assertEqual(len(audio), 160)
        self.assertAlmostEqual(float(audio.mean()), 0.25, delta=0.02)

    def test_webrtc_session_lifecycle_events_use_transport_descriptor_route(self) -> None:
        events = EventStore(max_context_events=20)
        session = WebRTCCallSession(
            call_id="webrtc-call-1",
            session_id="session-1",
            settings=Settings(greet_on_connect=False),
            event_store=events,
            stt=FakeSTT(),
            tts=FakeTTS(),
            metadata={"workspace_id": "workspace-1", "voicebot_id": "voicebot-1", "source": "browser"},
        )
        self.addCleanup(session.stop)

        session.start()
        snapshot = session.snapshot()

        lifecycle = events.list_events(call_id="webrtc-call-1")
        self.assertEqual([event.type for event in lifecycle], ["call_started", "call_connected"])
        self.assertEqual(lifecycle[0].data["transport"], "webrtc")
        self.assertEqual(lifecycle[0].data["sample_rate"], STT_SAMPLE_RATE)
        self.assertEqual(lifecycle[0].data["pipeline_version"], PIPELINE_CONTRACT_VERSION)
        self.assertEqual(lifecycle[0].data["workspace_id"], "workspace-1")
        self.assertEqual(lifecycle[0].data["voicebot_id"], "voicebot-1")
        self.assertEqual(lifecycle[0].data["metadata"], {"source": "browser", "session_id": "session-1"})
        self.assertEqual(snapshot["route"]["workspace_id"], "workspace-1")
        self.assertEqual(snapshot["pipeline_version"], PIPELINE_CONTRACT_VERSION)
        self.assertIn("hangup", snapshot["capabilities"]["call_control"])

    def test_webrtc_remote_audio_uses_jitter_buffer_before_vad(self) -> None:
        events = EventStore(max_context_events=20)
        session = WebRTCCallSession(
            call_id="webrtc-call-1",
            session_id="session-1",
            settings=Settings(
                greet_on_connect=False,
                start_threshold=0.1,
                stop_threshold=0.05,
                vad_start_ms=0,
                packet_ms=20,
                webrtc_jitter_target_delay_ms=40,
                webrtc_jitter_max_delay_ms=80,
            ),
            event_store=events,
            stt=FakeSTT(),
            tts=FakeTTS(),
        )
        self.addCleanup(session.stop)
        block = np.full(320, 0.4, dtype=np.float32)

        self.assertEqual(session.process_remote_audio_block(block), 0)
        self.assertEqual(session.process_remote_audio_block(block), 0)
        self.assertEqual(session.process_remote_audio_block(block), 1)

        self.assertEqual(
            [event.type for event in events.list_events(call_id="webrtc-call-1") if event.type == "user_speech_started"],
            ["user_speech_started"],
        )
        self.assertTrue(session.snapshot()["jitter_buffer"]["enabled"])

    def test_webrtc_remote_audio_can_bypass_jitter_buffer(self) -> None:
        events = EventStore(max_context_events=20)
        session = WebRTCCallSession(
            call_id="webrtc-call-1",
            session_id="session-1",
            settings=Settings(
                greet_on_connect=False,
                start_threshold=0.1,
                stop_threshold=0.05,
                vad_start_ms=0,
                webrtc_jitter_buffer_enabled=False,
            ),
            event_store=events,
            stt=FakeSTT(),
            tts=FakeTTS(),
        )
        self.addCleanup(session.stop)

        self.assertEqual(session.process_remote_audio_block(np.full(160, 0.4, dtype=np.float32)), 1)

        self.assertFalse(session.snapshot()["jitter_buffer"]["enabled"])
        self.assertEqual(
            [event.type for event in events.list_events(call_id="webrtc-call-1") if event.type == "user_speech_started"],
            ["user_speech_started"],
        )

    def test_webrtc_vad_decisions_emit_runtime_metrics(self) -> None:
        events = EventStore(max_context_events=20)
        session = WebRTCCallSession(
            call_id="webrtc-call-1",
            session_id="session-1",
            settings=Settings(
                greet_on_connect=False,
                start_threshold=0.1,
                stop_threshold=0.05,
                vad_start_ms=0,
                silence_ms=10,
                min_seconds=999.0,
                max_seconds=1000.0,
            ),
            event_store=events,
            stt=FakeSTT(),
            tts=FakeTTS(),
        )
        self.addCleanup(session.stop)

        session.process_audio_block(np.full(160, 0.4, dtype=np.float32))
        session.process_audio_block(np.zeros(160, dtype=np.float32))

        metrics = [event.data for event in events.list_events(call_id="webrtc-call-1") if event.type == "metrics"]
        vad_decisions = [metric for metric in metrics if metric["name"] == "vad_decision"]
        self.assertEqual([metric["decision"] for metric in vad_decisions], ["speech_started", "speech_too_short"])
        self.assertEqual(vad_decisions[0]["transport"], "webrtc")
        self.assertIn({"name": "silence_duration_seconds", "value": 0.01, "turn_id": 1}, metrics)


if __name__ == "__main__":
    unittest.main()
