from __future__ import annotations

import html
import re
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
    def test_root_redirects_to_dashboard(self) -> None:
        client = self.client()

        response = client.get("/", follow_redirects=False)

        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "/dashboard")

    def test_dashboard_page_and_state_are_internal_workspace_scoped(self) -> None:
        client = self.client()

        page = client.get("/dashboard")
        state = client.get("/dashboard/state?workspace_id=workspace-1")

        self.assertEqual(page.status_code, 200)
        self.assertIn("FlowHunt Voicebot Dashboard", page.text)
        self.assertIn("Workspaces", page.text)
        self.assertNotIn("Active Sessions", page.text)
        self.assertIn("Sessions History", page.text)
        self.assertIn("Voicebot Test", page.text)
        self.assertIn('data-table-filter="workspace-rows"', page.text)
        self.assertIn('data-table-filter="voicebot-rows"', page.text)
        self.assertIn('data-table-filter="history-session-rows"', page.text)
        self.assertIn('id="history-status-filter"', page.text)
        self.assertIn("renderHistoryStatusOptions(items)", page.text)
        self.assertIn('data-table-filter="session-event-rows"', page.text)
        self.assertIn('data-table-filter="session-transcript-rows"', page.text)
        self.assertIn('data-table-filter="session-chat-rows"', page.text)
        self.assertIn('id="prompt-filler"', page.text)
        self.assertIn('id="prompt-colleague-progress"', page.text)
        self.assertIn('id="prompt-chat-mode"', page.text)
        self.assertIn('id="prompt-chat-system"', page.text)
        self.assertIn('id="prompt-chat-response"', page.text)
        self.assertIn('id="prompt-chat-rich-content"', page.text)
        self.assertIn('id="prompt-subagent-prompts"', page.text)
        self.assertIn('id="provider-tts-model"', page.text)
        self.assertIn('id="provider-tts-voice"', page.text)
        self.assertIn('id="provider-stt-provider"', page.text)
        self.assertIn('id="provider-agent-provider"', page.text)
        self.assertIn('id="runtime-realtime"', page.text)
        self.assertIn('id="runtime-channels"', page.text)
        self.assertIn('id="runtime-quotas"', page.text)
        self.assertIn('id="runtime-subagents"', page.text)
        self.assertIn('id="save-providers"', page.text)
        self.assertIn('id="save-runtime"', page.text)
        self.assertIn('id="provider-config-note"', page.text)
        self.assertIn('id="runtime-config-note"', page.text)
        self.assertIn("defaultProviderConfig()", page.text)
        self.assertIn("defaultRuntimeConfig()", page.text)
        self.assertIn("Provider config is not saved yet", page.text)
        self.assertIn("Runtime config is not saved yet", page.text)
        self.assertIn("buildProviderPayloadFromEditor()", page.text)
        self.assertIn("providerChoiceFromEditor", page.text)
        self.assertIn("applyTableFilter(tbodyId)", page.text)
        self.assertIn('id="session-gantt"', page.text)
        self.assertIn('id="session-gantt-detail"', page.text)
        self.assertIn('id="session-gantt-dialog"', page.text)
        self.assertIn('id="session-gantt-dialog-close"', page.text)
        self.assertIn('data-session-tab="timeline"', page.text)
        self.assertIn('data-session-tab="chat"', page.text)
        self.assertIn('id="session-tab-recording"', page.text)
        self.assertIn('id="session-tab-chat"', page.text)
        self.assertIn("showSessionTab(button.dataset.sessionTab)", page.text)
        self.assertIn("session_tab: sessionTab", page.text)
        self.assertIn("session_tab: params.get(\"session_tab\")", page.text)
        self.assertIn("currentSessionRoute", page.text)
        self.assertIn("renderSessionGantt(timeline.events || [])", page.text)
        self.assertIn("showGanttEventDetails(item, bar)", page.text)
        self.assertIn('for (const item of items)', page.text)
        self.assertIn(".showModal()", page.text)
        self.assertIn('aria-label="Session transcript"', page.text)
        self.assertIn('aria-label="Session chat widget communication"', page.text)
        self.assertIn("renderSessionTranscript(transcript.events || [])", page.text)
        self.assertIn("renderSessionChat(timeline.events || [])", page.text)
        self.assertIn("isSessionChatEvent(event)", page.text)
        self.assertIn("renderSessionMarkdown(item.text)", page.text)
        self.assertIn('["timeline", "recording", "transcript", "chat", "events"]', page.text)
        self.assertIn("eventSummary(event)", page.text)
        self.assertIn("formatTimeOnly(event.timestamp)", page.text)
        self.assertIn("appendJsonBlockValue(pre, value, 0)", page.text)
        self.assertIn('className = "json-value json-string"', page.text)
        self.assertIn('className = "json-key"', page.text)
        self.assertIn("background:#f6f8fa; color:#24292f;", page.text)
        self.assertNotIn("background:#0d1117; color:#c9d1d9;", page.text)
        self.assertIn(".session-layout { display:grid; grid-template-columns:1fr;", page.text)
        self.assertNotIn("grid-template-columns:minmax(0,1.45fr) minmax(22rem,.75fr)", page.text)
        self.assertNotIn("session-stack", page.text)
        self.assertIn("ganttEventLane(event)", page.text)
        self.assertIn("ganttEventTypeClass(event)", page.text)
        self.assertIn("gantt-type-call-connected", page.text)
        self.assertIn("gantt-type-security", page.text)
        self.assertIn(".gantt-bar.system", page.text)
        self.assertIn('typeClass: ganttEventTypeClass(event)', page.text)
        self.assertIn("isPostCallDashboardAuditEvent(event)", page.text)
        self.assertIn('item.time <= callEndTime + 1000', page.text)
        self.assertIn("dashboardRouteFromLocation()", page.text)
        self.assertIn("applyDashboardRoute(route)", page.text)
        self.assertIn("updateDashboardUrl({", page.text)
        self.assertIn("window.addEventListener(\"hashchange\", handleDashboardLocationChange)", page.text)
        self.assertIn("window.addEventListener(\"popstate\", handleDashboardLocationChange)", page.text)
        self.assertIn("session_id: item.session_id || \"\"", page.text)
        self.assertIn("subagent_prompts_json", page.text)
        self.assertIn("(currentRuntimeConfig || defaultRuntimeConfig()).subagents?.prompts", page.text)
        self.assertIn("chat_enabled: false", page.text)
        self.assertIn("channels_json", page.text)
        self.assertIn("prompts: subagentPrompts", page.text)
        self.assertIn("srcdoc=", page.text)
        self.assertNotIn('src="/webrtc/test"', page.text)
        srcdoc = self.embedded_webrtc_srcdoc(page.text)
        self.assertIn('startButton.onclick = async () => {', srcdoc)
        self.assertIn('fetch("/webrtc/sessions"', srcdoc)
        self.assertIn('split(/\\r?\\n/)', srcdoc)
        self.assertNotIn('split(/\r?\n/)', srcdoc)
        self.assertEqual(state.status_code, 200)
        payload = state.json()
        self.assertEqual(payload["selected_workspace_id"], "workspace-1")
        self.assertEqual(payload["dashboard"]["webrtc_console"], "embedded")
        self.assertEqual(payload["workspace_rows"][0]["workspace_id"], "workspace-1")
        self.assertEqual(payload["voicebots"][0]["voicebot_id"], "voicebot-1")
        self.assertEqual(payload["voicebots"][0]["active_sessions"], 1)
        self.assertEqual(payload["active_sessions"][0]["workspace_id"], "workspace-1")
        self.assertEqual(payload["active_sessions"][0]["voicebot_id"], "voicebot-1")
        self.assertEqual(payload["session_history"][0]["status"], "active")
        self.assertEqual(payload["voicebots"][0]["channels"][0]["channel_id"], "channel-1")
        self.assertEqual(payload["voicebots"][0]["public_routes"][0]["route_id"], "route-1")

    def test_local_dashboard_state_recovers_from_stale_workspace_selection(self) -> None:
        client = self.client()

        response = client.get("/dashboard/state?workspace_id=missing-workspace")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["selected_workspace_id"], "workspace-1")

    def embedded_webrtc_srcdoc(self, page_text: str) -> str:
        match = re.search(r'srcdoc="(.*?)"', page_text, flags=re.DOTALL)
        self.assertIsNotNone(match)
        return html.unescape(match.group(1))

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
