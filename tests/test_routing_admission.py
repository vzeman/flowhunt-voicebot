from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.calls import CallRegistry
from voicebot.events import EventStore
from voicebot.provider_config import ProviderChoice, ProviderConfigStore, VoicebotProviderConfig
from voicebot.routing_admission import IncomingSessionRequest, evaluate_incoming_session
from voicebot.runtime_config import VoicebotRuntimeConfigStore
from voicebot.session_leases import SessionLeaseStore
from voicebot.transcripts import TranscriptStore
from voicebot.workspace_model import ChannelResolver, VoicebotChannelBinding, VoicebotDefinition, VoicebotStore
from voicebot.workspace_access import WorkspaceAccessPolicy


def provider_config() -> VoicebotProviderConfig:
    return VoicebotProviderConfig(
        workspace_id="workspace-1",
        voicebot_id="voicebot-1",
        stt=ProviderChoice("stt", "whisper"),
        tts=ProviderChoice("tts", "supertonic"),
        agent=ProviderChoice("agent", "local-codex"),
    )


def configured_stores() -> tuple[ChannelResolver, VoicebotStore, ProviderConfigStore, SessionLeaseStore]:
    channels = ChannelResolver(
        [
            VoicebotChannelBinding(
                channel_id="channel-1",
                kind="webrtc_widget",
                workspace_id="workspace-1",
                voicebot_id="voicebot-1",
                external_id="widget-1",
            )
        ]
    )
    voicebots = VoicebotStore()
    voicebots.create(VoicebotDefinition("workspace-1", "voicebot-1", enabled=True))
    providers = ProviderConfigStore()
    providers.save(provider_config())
    return channels, voicebots, providers, SessionLeaseStore()


class RoutingAdmissionTests(unittest.TestCase):
    def test_incoming_session_admission_accepts_and_acquires_lease(self) -> None:
        channels, voicebots, providers, leases = configured_stores()

        decision = evaluate_incoming_session(
            IncomingSessionRequest(
                channel_kind="webrtc_widget",
                external_id="widget-1",
                session_id="session-1",
                owner="worker-1",
                transport="webrtc",
            ),
            channel_resolver=channels,
            voicebot_store=voicebots,
            provider_config_store=providers,
            runtime_config_store=VoicebotRuntimeConfigStore(),
            workspace_access_policy=WorkspaceAccessPolicy(),
            session_lease_store=leases,
            active_session_snapshots=[],
        )

        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["decision"], "accept")
        self.assertEqual(decision["workspace_id"], "workspace-1")
        self.assertEqual(decision["lease"]["owner"], "worker-1")

    def test_incoming_session_admission_rejects_missing_provider_config(self) -> None:
        channels, voicebots, _providers, leases = configured_stores()

        decision = evaluate_incoming_session(
            IncomingSessionRequest("webrtc_widget", "widget-1", "session-1", "worker-1", "webrtc"),
            channel_resolver=channels,
            voicebot_store=voicebots,
            provider_config_store=ProviderConfigStore(),
            runtime_config_store=VoicebotRuntimeConfigStore(),
            workspace_access_policy=WorkspaceAccessPolicy(),
            session_lease_store=leases,
            active_session_snapshots=[],
        )

        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["reason"], "provider_config_missing")

    def test_incoming_session_admission_returns_webrtc_capacity_fallback(self) -> None:
        channels, voicebots, providers, leases = configured_stores()

        decision = evaluate_incoming_session(
            IncomingSessionRequest(
                "webrtc_widget",
                "widget-1",
                "session-1",
                "worker-1",
                "webrtc",
                max_concurrent_sessions=1,
            ),
            channel_resolver=channels,
            voicebot_store=voicebots,
            provider_config_store=providers,
            runtime_config_store=VoicebotRuntimeConfigStore(),
            workspace_access_policy=WorkspaceAccessPolicy(),
            session_lease_store=leases,
            active_session_snapshots=[
                {"call_id": "call-1", "route": {"workspace_id": "workspace-1", "voicebot_id": "voicebot-1"}}
            ],
        )

        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["fallback"]["kind"], "http_error_before_sdp_answer")

    def test_routing_admission_endpoint_emits_event_and_returns_decision(self) -> None:
        channels, voicebots, providers, leases = configured_stores()
        events = EventStore(max_context_events=20)
        app = create_app(
            events,
            CallRegistry(),
            AgentTaskTracker(),
            WebSocketHub(),
            TranscriptStore("/tmp/flowhunt-voicebot-test-transcripts"),
            None,
            provider_configs=providers,
            voicebots=voicebots,
            channels=channels,
            session_leases=leases,
        )
        client = TestClient(app)

        response = client.post(
            "/routing/admission",
            json={
                "channel_kind": "webrtc_widget",
                "external_id": "widget-1",
                "session_id": "session-1",
                "owner": "worker-1",
                "transport": "webrtc",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["allowed"])
        self.assertEqual([event.type for event in events.list_events(call_id="session-1")], ["session_admission_decided"])


if __name__ == "__main__":
    unittest.main()
