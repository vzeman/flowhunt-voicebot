from __future__ import annotations

import tempfile
import unittest

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.calls import CallRegistry
from voicebot.config import Settings
from voicebot.deployment_topology import deployment_topology_payload, enabled_role_names, role_readiness_payload
from voicebot.events import EventStore
from voicebot.health import readiness_report
from voicebot.transcripts import TranscriptStore


class DeploymentTopologyTests(unittest.TestCase):
    def test_all_runtime_roles_enables_full_role_catalog(self) -> None:
        settings = Settings(runtime_roles=("all",))

        payload = deployment_topology_payload(settings)

        self.assertEqual(payload["mode"], "all_in_one")
        self.assertEqual(payload["unknown_roles"], [])
        roles = {role["role"]: role for role in payload["roles"]}
        self.assertTrue(roles["api_control_plane"]["enabled"])
        self.assertTrue(roles["sip_media_ingress"]["enabled"])
        self.assertTrue(roles["post_call_worker"]["enabled"])
        services = {service["service"]: service for service in payload["target_services"]}
        self.assertEqual(services["voicebot-public-api"]["openapi_spec"], "/openapi/public.json")
        self.assertEqual(services["voicebot-internal-api"]["authentication"], "required internal API key or future service identity")
        self.assertIn("voicebot-sip-media", {port["service"] for port in payload["port_matrix"]})
        self.assertFalse(payload["future_kubernetes"]["manifests_included"])

    def test_runtime_roles_can_select_subset_and_report_unknown_values(self) -> None:
        settings = Settings(runtime_roles=("api_control_plane", "subagent_task_poller", "unknown"))

        self.assertEqual(enabled_role_names(settings), ("api_control_plane", "subagent_task_poller"))
        payload = deployment_topology_payload(settings)

        self.assertEqual(payload["mode"], "role_filtered")
        self.assertEqual(payload["unknown_roles"], ["unknown"])
        roles = {role["role"]: role for role in payload["roles"]}
        self.assertTrue(roles["api_control_plane"]["enabled"])
        self.assertFalse(roles["sip_media_ingress"]["enabled"])
        services = {service["service"]: service for service in payload["target_services"]}
        self.assertTrue(services["voicebot-internal-api"]["enabled"])
        self.assertFalse(services["voicebot-sip-media"]["enabled"])

    def test_public_ingress_boundary_never_exposes_internal_surfaces(self) -> None:
        payload = deployment_topology_payload(Settings(runtime_roles=("all",)))

        public = next(boundary for boundary in payload["ingress_boundaries"] if boundary["name"] == "public-web")

        self.assertEqual(public["allowed_route_audiences"], ["public"])
        self.assertIn("internal OpenAPI", public["forbidden_surfaces"])
        self.assertIn("dashboard", public["forbidden_surfaces"])
        self.assertIn("task queues", public["forbidden_surfaces"])

    def test_role_readiness_maps_existing_checks_to_enabled_roles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(runtime_roles=("api_control_plane",))
            readiness = readiness_report(
                transcripts=TranscriptStore(directory),
                asterisk=None,
                active_call_ids=[],
                storage_components={"events": EventStore(max_context_events=20)},
                settings=settings,
            )

        payload = role_readiness_payload(settings, readiness)

        api_role = next(role for role in payload["roles"] if role["role"] == "api_control_plane")
        sip_role = next(role for role in payload["roles"] if role["role"] == "sip_media_ingress")
        self.assertTrue(api_role["enabled"])
        self.assertTrue(api_role["ok"])
        self.assertFalse(sip_role["enabled"])
        self.assertFalse(sip_role["ok"])
        self.assertTrue(payload["routing"]["internal_api"]["safe"])
        self.assertFalse(payload["routing"]["public_http_webrtc"]["safe"])


class DeploymentTopologyApiTests(unittest.TestCase):
    def test_deployment_topology_and_role_readiness_endpoints(self) -> None:
        app = create_app(
            EventStore(max_context_events=20),
            CallRegistry(),
            AgentTaskTracker(),
            WebSocketHub(),
            TranscriptStore(tempfile.mkdtemp()),
            None,
            settings=Settings(runtime_roles=("api_control_plane",)),
        )
        client = TestClient(app)

        topology = client.get("/deployment/topology")
        readiness = client.get("/health/readiness/roles")

        self.assertEqual(topology.status_code, 200)
        self.assertEqual(readiness.status_code, 200)
        self.assertEqual(topology.json()["configured_roles"], ["api_control_plane"])
        self.assertEqual(readiness.json()["configured_roles"], ["api_control_plane"])


if __name__ == "__main__":
    unittest.main()
