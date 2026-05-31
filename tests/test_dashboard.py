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
        self.assertIn("Workspaces", page.text)
        self.assertIn("Active Sessions", page.text)
        self.assertIn("Sessions History", page.text)
        self.assertIn("Voicebot Test", page.text)
        self.assertIn('data-table-filter="workspace-rows"', page.text)
        self.assertIn('data-table-filter="voicebot-rows"', page.text)
        self.assertIn('data-table-filter="active-session-rows"', page.text)
        self.assertIn('data-table-filter="history-session-rows"', page.text)
        self.assertIn('data-table-filter="session-event-rows"', page.text)
        self.assertIn('data-table-filter="session-transcript-rows"', page.text)
        self.assertIn("applyTableFilter(tbodyId)", page.text)
        self.assertIn('id="session-gantt"', page.text)
        self.assertIn('id="session-gantt-detail"', page.text)
        self.assertIn('id="session-gantt-dialog"', page.text)
        self.assertIn('id="session-gantt-dialog-close"', page.text)
        self.assertIn("renderSessionGantt(timeline.events || [])", page.text)
        self.assertIn("showGanttEventDetails(item, bar)", page.text)
        self.assertIn('for (const item of items)', page.text)
        self.assertIn(".showModal()", page.text)
        self.assertIn('aria-label="Session transcript"', page.text)
        self.assertIn("renderSessionTranscript(transcript.events || [])", page.text)
        self.assertIn("eventSummary(event)", page.text)
        self.assertIn("formatTimeOnly(event.timestamp)", page.text)
        self.assertIn("appendJsonBlockValue(pre, value, 0)", page.text)
        self.assertIn('className = "json-value json-string"', page.text)
        self.assertIn('className = "json-key"', page.text)
        self.assertIn(".session-layout { display:grid; grid-template-columns:1fr;", page.text)
        self.assertNotIn("grid-template-columns:minmax(0,1.45fr) minmax(22rem,.75fr)", page.text)
        self.assertNotIn("session-stack", page.text)
        self.assertIn("ganttEventLane(event)", page.text)
        self.assertIn("isPostCallDashboardAuditEvent(event)", page.text)
        self.assertIn('item.time <= callEndTime + 1000', page.text)
        self.assertIn("srcdoc=", page.text)
        self.assertNotIn('src="/webrtc/test"', page.text)
        self.assertEqual(state.status_code, 200)
        payload = state.json()
        self.assertEqual(payload["selected_workspace_id"], "workspace-1")
        self.assertEqual(payload["dashboard"]["webrtc_console"], "embedded")
        self.assertEqual(payload["workspace_rows"][0]["workspace_id"], "workspace-1")
        self.assertEqual(payload["voicebots"][0]["voicebot_id"], "voicebot-1")
        self.assertEqual(payload["voicebots"][0]["active_sessions"], 1)
        self.assertEqual(payload["active_sessions"][0]["workspace_id"], "workspace-1")
        self.assertEqual(payload["active_sessions"][0]["voicebot_id"], "voicebot-1")
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

    def test_dashboard_user_auth_filters_workspaces(self) -> None:
        client = self.client(Settings(dashboard_auth_enabled=True))

        missing = client.get("/dashboard/state")
        allowed = client.get(
            "/dashboard/state?workspace_id=workspace-1",
            headers={"X-FlowHunt-User-Id": "user-1", "X-FlowHunt-Workspace-Ids": "workspace-1"},
        )
        denied = client.get(
            "/dashboard/state?workspace_id=workspace-1",
            headers={"X-FlowHunt-User-Id": "user-1", "X-FlowHunt-Workspace-Ids": "other-workspace"},
        )
        service_key_only = client.get("/dashboard/state", headers={"X-FlowHunt-Internal-Key": "secret"})

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.json()["dashboard"]["user"]["user_id"], "user-1")
        self.assertEqual(allowed.json()["workspaces"], ["workspace-1"])
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(service_key_only.status_code, 401)

    def test_dashboard_dev_login_bypass_is_explicit_and_local_only(self) -> None:
        client = self.client(Settings(dashboard_auth_enabled=True, dashboard_dev_login_enabled=True, deployment_mode="local"))
        production_client = self.client(
            Settings(dashboard_auth_enabled=True, dashboard_dev_login_enabled=True, deployment_mode="production")
        )

        allowed = client.get("/dashboard/state", headers={"X-FlowHunt-Dev-Login": "true"})
        denied_without_header = client.get("/dashboard/state")
        denied_production = production_client.get("/dashboard/state", headers={"X-FlowHunt-Dev-Login": "true"})

        self.assertEqual(allowed.status_code, 200)
        self.assertTrue(allowed.json()["dashboard"]["user"]["dev_login"])
        self.assertEqual(denied_without_header.status_code, 401)
        self.assertEqual(denied_production.status_code, 401)

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
