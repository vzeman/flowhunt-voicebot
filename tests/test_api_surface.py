from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.api_surface import (
    ApiEndpointSpec,
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

    def test_api_endpoint_spec_rejects_invalid_contract_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported API method"):
            ApiEndpointSpec("TRACE", "/x", "admin", "public")
        with self.assertRaisesRegex(ValueError, "path"):
            ApiEndpointSpec("GET", "x", "admin", "public")
        with self.assertRaisesRegex(ValueError, "unsupported API area"):
            ApiEndpointSpec("GET", "/x", "unknown", "public")
        with self.assertRaisesRegex(ValueError, "unsupported API visibility"):
            ApiEndpointSpec("GET", "/x", "admin", "external")
        with self.assertRaisesRegex(ValueError, "unsupported API scope source"):
            ApiEndpointSpec("GET", "/x", "admin", "public", scope_source="header")
        with self.assertRaisesRegex(ValueError, "scope_source=none"):
            ApiEndpointSpec("GET", "/health", "internal", "internal", workspace_scoped=False, scope_source="query")

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
            "security",
            "multimodal",
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
        self.assertEqual(response.json()["route_audiences"][0]["audience"] in {"public", "internal", "local_dev"}, True)

    def test_all_http_routes_have_audience_metadata(self) -> None:
        app = self.build_client().app

        self.assertEqual(getattr(app.state, "route_audience_issues", []), [])

    def test_public_openapi_excludes_internal_and_local_routes(self) -> None:
        response = self.build_client().get("/openapi/public.json")

        self.assertEqual(response.status_code, 200)
        paths = set(response.json()["paths"])
        self.assertIn("/webrtc/sessions", paths)
        self.assertEqual(set(response.json()["paths"]["/webrtc/sessions"]), {"post"})
        self.assertIn("/health", paths)
        self.assertNotIn("/agent/tasks", paths)
        self.assertNotIn("/webrtc/test", paths)
        self.assertNotIn("/config", paths)

    def test_internal_openapi_excludes_public_session_creation_but_keeps_local_dev_tools(self) -> None:
        response = self.build_client().get("/openapi/internal.json")

        self.assertEqual(response.status_code, 200)
        paths = set(response.json()["paths"])
        self.assertIn("/agent/tasks", paths)
        self.assertIn("/webrtc/test", paths)
        self.assertIn("/config", paths)
        self.assertNotIn("post", response.json()["paths"]["/webrtc/sessions"])
        self.assertIn("get", response.json()["paths"]["/webrtc/sessions"])

    def test_runtime_endpoint_declares_payload_workspace_scope(self) -> None:
        grouped = api_surface_by_area()
        runtime = next(endpoint for endpoint in grouped["runtime"] if endpoint["path"] == "/runtime/webrtc/sessions")

        self.assertEqual(runtime["scope_source"], "payload")
        self.assertTrue(runtime["workspace_scoped"])

    def test_deployment_topology_endpoints_are_internal_unscoped(self) -> None:
        grouped = api_surface_by_area()
        runtime_paths = {endpoint["path"]: endpoint for endpoint in grouped["runtime"]}

        self.assertFalse(runtime_paths["/deployment/topology"]["workspace_scoped"])
        self.assertFalse(runtime_paths["/health/readiness/roles"]["workspace_scoped"])

    def test_multimodal_endpoints_are_cataloged(self) -> None:
        grouped = api_surface_by_area()
        paths = {endpoint["path"]: endpoint for endpoint in grouped["multimodal"]}

        self.assertEqual(paths["/calls/{call_id}/multimodal"]["scope_source"], "route_binding")
        self.assertEqual(paths["/calls/{call_id}/multimodal/parts"]["scope_source"], "payload")

    def test_security_endpoints_are_cataloged(self) -> None:
        grouped = api_surface_by_area()
        paths = {endpoint["path"]: endpoint for endpoint in grouped["security"]}

        self.assertEqual(paths["/security/contract"]["scope_source"], "none")
        self.assertEqual(paths["/workspaces/{workspace_id}/security/audit"]["scope_source"], "path")
        self.assertTrue(paths["/workspaces/{workspace_id}/security/retention"]["workspace_scoped"])

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
