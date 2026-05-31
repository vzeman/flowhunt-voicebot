from __future__ import annotations

import tempfile
import unittest

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.calls import CallRegistry
from voicebot.config import Settings
from voicebot.events import EventStore
from voicebot.transcripts import TranscriptStore
from voicebot.workspace_model import (
    ChannelResolver,
    PublicVoicebotRoute,
    PublicVoicebotRouteStore,
    VoicebotChannelBinding,
    VoicebotDefinition,
    VoicebotStore,
)


class FakeWebRTCManager:
    def snapshots(self):
        return [
            {
                "session_id": "session-1",
                "call_id": "webrtc-session-1",
                "connection_state": "connected",
                "metadata": {"workspace_id": "workspace-1", "voicebot_id": "voicebot-1"},
            }
        ]


class DashboardTests(unittest.TestCase):
    def test_dashboard_page_and_state_are_internal_workspace_scoped(self) -> None:
        client = self.client()

        page = client.get("/dashboard")
        state = client.get("/dashboard/state?workspace_id=workspace-1")

        self.assertEqual(page.status_code, 200)
        self.assertIn("FlowHunt Voicebot Dashboard", page.text)
        self.assertIn('/webrtc/test', page.text)
        self.assertEqual(state.status_code, 200)
        payload = state.json()
        self.assertEqual(payload["selected_workspace_id"], "workspace-1")
        self.assertEqual(payload["voicebots"][0]["voicebot_id"], "voicebot-1")
        self.assertEqual(payload["voicebots"][0]["active_sessions"], 1)
        self.assertEqual(payload["voicebots"][0]["channels"][0]["channel_id"], "channel-1")
        self.assertEqual(payload["voicebots"][0]["public_routes"][0]["route_id"], "route-1")

    def test_dashboard_requires_internal_auth_when_enabled(self) -> None:
        client = self.client(
            Settings(internal_auth_enabled=True, internal_api_keys=("dashboard:ops:secret:dashboard:read",))
        )

        missing = client.get("/dashboard")
        allowed = client.get("/dashboard", headers={"X-FlowHunt-Internal-Key": "secret"})

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(allowed.status_code, 200)

    def client(self, settings: Settings | None = None) -> TestClient:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        voicebots = VoicebotStore()
        voicebots.create(VoicebotDefinition("workspace-1", "voicebot-1", display_name="Support", enabled=True))
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
                )
            ]
        )
        app = create_app(
            EventStore(max_context_events=20),
            CallRegistry(),
            AgentTaskTracker(),
            WebSocketHub(),
            TranscriptStore(directory.name),
            None,
            settings=settings,
            webrtc=FakeWebRTCManager(),
            voicebots=voicebots,
            channels=channels,
            public_routes=routes,
        )
        return TestClient(app)


if __name__ == "__main__":
    unittest.main()
