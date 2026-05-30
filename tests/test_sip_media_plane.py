from __future__ import annotations

import tempfile
import unittest

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.calls import CallRegistry
from voicebot.events import EventStore
from voicebot.sip_media_plane import sip_media_plane_issues, sip_media_plane_payload
from voicebot.transcripts import TranscriptStore


class SipMediaPlaneTests(unittest.TestCase):
    def test_sip_media_plane_contract_is_valid(self) -> None:
        self.assertEqual(sip_media_plane_issues(), [])

    def test_sip_media_plane_contract_makes_failover_boundary_explicit(self) -> None:
        payload = sip_media_plane_payload()

        self.assertEqual(payload["architecture"]["active_call_failover"], "interrupted_not_migrated")
        self.assertEqual(payload["routing"]["workspace_scope"], ["workspace_id", "voicebot_id", "trunk_id"])
        self.assertEqual(payload["routing"]["session_owner"], "session lease owner receives media/control actions")

    def test_sip_media_plane_endpoint_returns_contract(self) -> None:
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

            response = client.get("/sip/media-plane")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), sip_media_plane_payload())
