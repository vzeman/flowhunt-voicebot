from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.api_surface import api_surface_by_area, prototype_endpoints, public_endpoints_are_workspace_scoped
from voicebot.calls import CallRegistry
from voicebot.events import EventStore
from voicebot.transcripts import TranscriptStore


class ApiSurfaceTests(unittest.TestCase):
    def test_public_endpoints_are_workspace_scoped(self) -> None:
        self.assertTrue(public_endpoints_are_workspace_scoped())

    def test_api_surface_covers_required_areas(self) -> None:
        grouped = api_surface_by_area()

        for area in ("admin", "channel", "runtime", "session", "transcript", "task", "provider", "testing"):
            self.assertIn(area, grouped)

    def test_prototype_endpoints_are_identified(self) -> None:
        endpoints = prototype_endpoints()

        self.assertEqual(len(endpoints), 1)
        self.assertEqual(endpoints[0]["path"], "/webrtc/test")
        self.assertEqual(endpoints[0]["visibility"], "prototype")

    def test_api_surface_endpoint_exposes_grouped_catalog(self) -> None:
        response = self.build_client().get("/api/surface")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["public_endpoints_are_workspace_scoped"])
        self.assertIn("admin", response.json()["areas"])

    def test_api_surface_prototypes_endpoint_lists_prototypes(self) -> None:
        response = self.build_client().get("/api/surface/prototypes")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["endpoints"][0]["path"], "/webrtc/test")

    def build_client(self) -> TestClient:
        app = create_app(
            EventStore(max_context_events=20),
            CallRegistry(),
            AgentTaskTracker(),
            WebSocketHub(),
            TranscriptStore("/tmp/flowhunt-voicebot-test-transcripts"),
            None,
        )
        return TestClient(app)


if __name__ == "__main__":
    unittest.main()
