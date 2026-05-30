from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.asterisk_control import AsteriskAMI
from voicebot.calls import CallRegistry
from voicebot.events import EventStore
from voicebot.health import ami_configuration_check, durable_storage_check, pipeline_contract_check, readiness_report, storage_contract_check
from voicebot.pipeline_contract import pipeline_contract_payload
from voicebot.storage_contracts import storage_contracts_payload
from voicebot.transcripts import TranscriptStore


class HealthTests(unittest.TestCase):
    def test_readiness_report_includes_core_checks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = readiness_report(
                transcripts=TranscriptStore(directory),
                asterisk=None,
                active_call_ids=["call-a"],
                storage_components={"events": EventStore(max_context_events=20)},
            )

        self.assertTrue(report["ok"])
        self.assertEqual(report["active_calls"], ["call-a"])
        self.assertTrue(report["checks"]["transcripts"]["ok"])
        self.assertFalse(report["checks"]["ami"]["configured"])
        self.assertTrue(report["checks"]["providers"]["ok"])
        self.assertTrue(report["checks"]["event_catalog"]["ok"])
        self.assertTrue(report["checks"]["pipeline_contract"]["ok"])
        self.assertTrue(report["checks"]["storage_contracts"]["ok"])
        self.assertTrue(report["checks"]["durable_storage"]["ok"])

    def test_readiness_reports_transcript_corruption_stats(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "call-1.jsonl"
            path.write_text("not-json\n", encoding="utf-8")
            report = readiness_report(
                transcripts=TranscriptStore(directory),
                asterisk=None,
                active_call_ids=[],
            )

        check = report["checks"]["transcripts"]
        self.assertTrue(check["ok"])
        self.assertEqual(check["transcript_count"], 1)
        self.assertEqual(check["skipped_line_count"], 1)
        self.assertEqual(check["corrupt_transcript_count"], 1)
        self.assertEqual(check["corrupt_call_ids"], ["call-1"])

    def test_readiness_transcript_stats_are_not_limited(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcripts = TranscriptStore(directory)
            for index in range(3):
                transcripts.append(
                    type(
                        "Event",
                        (),
                        {
                            "id": index + 1,
                            "call_id": f"call-{index + 1}",
                            "type": "call_started",
                            "timestamp": "2026-05-27T00:00:00Z",
                            "data": {},
                        },
                    )()
                )
            report = readiness_report(
                transcripts=transcripts,
                asterisk=None,
                active_call_ids=[],
            )

        self.assertEqual(report["checks"]["transcripts"]["transcript_count"], 3)

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
        self.assertEqual(
            set(payload["checks"]),
            {"transcripts", "ami", "providers", "event_catalog", "pipeline_contract", "storage_contracts", "durable_storage"},
        )

    def test_pipeline_contract_check_exposes_valid_pipeline_catalog(self) -> None:
        check = pipeline_contract_check().to_dict()

        self.assertTrue(check["ok"])
        self.assertEqual(check["issue_count"], 0)
        self.assertEqual(check["version"], pipeline_contract_payload()["version"])
        self.assertIn("asterisk_audiosocket", check["transports"])
        self.assertIn("webrtc", check["transports"])

    def test_storage_contracts_are_exposed_and_valid(self) -> None:
        check = storage_contract_check().to_dict()

        self.assertTrue(check["ok"])
        self.assertEqual(check["issue_count"], 0)
        names = {contract["name"] for contract in check["contracts"]}
        self.assertIn("events", names)
        self.assertIn("session_leases", names)
        self.assertIn("provider_config", names)
        for contract in check["contracts"]:
            self.assertTrue(contract["required_scope_fields"])
            self.assertTrue(contract["idempotency_fields"])
            self.assertTrue(contract["production_backends"])

    def test_storage_contract_endpoint_returns_contract_catalog(self) -> None:
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

            response = client.get("/storage/contracts")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), storage_contracts_payload())

    def test_durable_storage_check_reports_load_diagnostics_and_counts_warnings(self) -> None:
        class StoreWithDiagnostics:
            load_diagnostics = {
                "loaded_events": 3,
                "skipped_malformed_json": 2,
                "requeued_expired_claims": 1,
            }

            def snapshot(self):
                return {
                    "pending": {"voicebot.agent": [{"item_id": "item-1"}]},
                    "claimed": [{"item": {"item_id": "item-2"}}],
                }

        check = durable_storage_check({"worker_queue": StoreWithDiagnostics()}).to_dict()

        self.assertTrue(check["ok"])
        self.assertEqual(check["message"], "durable storage is reachable with recovery warnings")
        self.assertEqual(check["warning_counts"], {"worker_queue": 3})
        store = check["stores"]["worker_queue"]
        self.assertEqual(store["kind"], "StoreWithDiagnostics")
        self.assertEqual(store["warning_count"], 3)
        self.assertEqual(store["snapshot"], {"pending_count": 1, "claimed_count": 1})

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
