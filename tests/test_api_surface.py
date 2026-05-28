from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.api_surface import (
    api_scope_violations,
    api_surface_by_area,
    api_surface_integrity_issues,
    api_surface_summary,
    prototype_endpoints,
    public_endpoints_are_workspace_scoped,
)
from voicebot.calls import CallRegistry
from voicebot.events import EventStore
from voicebot.transcripts import TranscriptStore


class ApiSurfaceTests(unittest.TestCase):
    def test_public_endpoints_are_workspace_scoped(self) -> None:
        self.assertTrue(public_endpoints_are_workspace_scoped())
        self.assertEqual(api_scope_violations(), [])

    def test_api_surface_catalog_has_no_integrity_issues(self) -> None:
        self.assertEqual(api_surface_integrity_issues(), [])

    def test_api_surface_summary_counts_catalog_dimensions(self) -> None:
        summary = api_surface_summary()

        self.assertEqual(summary["total"], sum(summary["by_area"].values()))
        self.assertEqual(summary["total"], sum(summary["by_visibility"].values()))
        self.assertEqual(summary["total"], sum(summary["by_scope_source"].values()))
        self.assertEqual(summary["by_visibility"]["prototype"], 1)

    def test_api_surface_covers_required_areas(self) -> None:
        grouped = api_surface_by_area()

        for area in (
            "admin",
            "channel",
            "runtime",
            "session",
            "transcript",
            "task",
            "provider",
            "transport",
            "scaling",
            "testing",
        ):
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
        self.assertEqual(response.json()["scope_violations"], [])
        self.assertEqual(response.json()["integrity_issues"], [])
        self.assertEqual(response.json()["summary"]["by_visibility"]["prototype"], 1)
        self.assertIn("admin", response.json()["areas"])

    def test_runtime_endpoint_declares_payload_workspace_scope(self) -> None:
        grouped = api_surface_by_area()
        runtime = next(endpoint for endpoint in grouped["runtime"] if endpoint["path"] == "/runtime/webrtc/sessions")

        self.assertEqual(runtime["scope_source"], "payload")
        self.assertTrue(runtime["workspace_scoped"])

    def test_api_surface_prototypes_endpoint_lists_prototypes(self) -> None:
        response = self.build_client().get("/api/surface/prototypes")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["endpoints"][0]["path"], "/webrtc/test")

    def test_voicebot_transport_catalog_endpoint_exposes_capabilities(self) -> None:
        response = self.build_client().get("/workspaces/workspace-1/voicebots/voicebot-1/transports")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["workspace_id"], "workspace-1")
        self.assertEqual(payload["voicebot_id"], "voicebot-1")
        self.assertIn("asterisk_audiosocket", payload["transports"])
        self.assertIn("hangup", payload["transports"]["webrtc"]["capabilities"]["call_control"])

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
