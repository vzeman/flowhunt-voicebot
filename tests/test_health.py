from __future__ import annotations

import tempfile
import unittest

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.asterisk_control import AsteriskAMI
from voicebot.calls import CallRegistry
from voicebot.events import EventStore
from voicebot.health import ami_configuration_check, readiness_report
from voicebot.transcripts import TranscriptStore


class HealthTests(unittest.TestCase):
    def test_readiness_report_includes_core_checks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = readiness_report(
                transcripts=TranscriptStore(directory),
                asterisk=None,
                active_call_ids=["call-a"],
            )

        self.assertTrue(report["ok"])
        self.assertEqual(report["active_calls"], ["call-a"])
        self.assertTrue(report["checks"]["transcripts"]["ok"])
        self.assertFalse(report["checks"]["ami"]["configured"])
        self.assertTrue(report["checks"]["providers"]["ok"])
        self.assertTrue(report["checks"]["event_catalog"]["ok"])

    def test_ami_check_reports_config_without_password(self) -> None:
        asterisk = AsteriskAMI("127.0.0.1", 5038, "admin", "secret")

        check = ami_configuration_check(asterisk).to_dict()

        self.assertTrue(check["ok"])
        self.assertTrue(check["configured"])
        self.assertEqual(check["host"], "127.0.0.1")
        self.assertEqual(check["port"], 5038)
        self.assertEqual(check["username"], "admin")
        self.assertNotIn("password", check)

    def test_readiness_endpoint_returns_structured_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = CallRegistry()
            app = create_app(
                EventStore(max_context_events=20),
                registry,
                AgentTaskTracker(),
                WebSocketHub(),
                TranscriptStore(directory),
                None,
            )
            client = TestClient(app)

            response = client.get("/health/readiness")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["active_calls"], [])
        self.assertEqual(set(payload["checks"]), {"transcripts", "ami", "providers", "event_catalog"})

    def test_existing_health_endpoint_remains_lightweight(self) -> None:
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

            response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "active_calls": []})


if __name__ == "__main__":
    unittest.main()
