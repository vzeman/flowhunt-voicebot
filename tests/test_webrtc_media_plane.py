from __future__ import annotations

import tempfile
import unittest

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.calls import CallRegistry
from voicebot.events import EventStore
from voicebot.transcripts import TranscriptStore
from voicebot.webrtc_media_plane import webrtc_media_plane_issues, webrtc_media_plane_payload


class WebRTCMediaPlaneTests(unittest.TestCase):
    def test_webrtc_media_plane_contract_is_valid(self) -> None:
        self.assertEqual(webrtc_media_plane_issues(), [])

    def test_webrtc_media_plane_contract_defines_routing_and_turn(self) -> None:
        payload = webrtc_media_plane_payload()

        self.assertEqual(payload["routing"]["workspace_scope"], ["workspace_id", "voicebot_id", "channel_id"])
        self.assertEqual(payload["ice"]["turn"], "required for production reliability")
        self.assertIsNone(payload["local_development"]["browser_test_page"])
        self.assertEqual(payload["local_development"]["dashboard_console"], "/dashboard")

    def test_webrtc_media_plane_endpoint_returns_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                EventStore(max_context_events=20),
                CallRegistry(),
                AgentTaskTracker(),
                WebSocketHub(),
                TranscriptStore(directory),
                None,
            )
            client = TestClient(app)

            response = client.get("/webrtc/media-plane")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), webrtc_media_plane_payload())
