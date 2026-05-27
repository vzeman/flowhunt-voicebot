from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from voicebot.api import AgentTaskTracker, WebSocketHub, create_app
from voicebot.calls import CallRegistry
from voicebot.events import EventStore
from voicebot.transcripts import TranscriptStore


class FakeAsterisk:
    def hangup(self, call_id: str):
        return FakeControlResult(True, f"hung up {call_id}")

    def transfer(self, call_id: str, target: str):
        return FakeControlResult(True, f"transferred {call_id} to {target}")


class FakeControlResult:
    def __init__(self, ok: bool, message: str) -> None:
        self.ok = ok
        self.message = message


class ApiCallControlTests(unittest.TestCase):
    def build_client(self, asterisk=None) -> tuple[TestClient, EventStore, AgentTaskTracker]:
        events = EventStore(max_context_events=20)
        tracker = AgentTaskTracker()
        app = create_app(
            events,
            CallRegistry(),
            tracker,
            WebSocketHub(),
            TranscriptStore("/tmp/flowhunt-voicebot-test-transcripts"),
            asterisk,
        )
        return TestClient(app), events, tracker

    def test_call_control_records_failure_when_ami_is_not_configured(self) -> None:
        client, events, tracker = self.build_client(asterisk=None)

        response = client.post(
            "/calls/call-1/control",
            json={"action": "hangup", "response_to_event_id": 42},
        )

        self.assertEqual(response.status_code, 503)
        self.assertIn(42, tracker.responded_event_ids)
        persisted = events.list_events(call_id="call-1")
        self.assertEqual([event.type for event in persisted], ["call_control_requested", "call_control_completed"])
        self.assertFalse(persisted[1].data["ok"])
        self.assertEqual(persisted[1].data["message"], "Asterisk AMI control is not configured")

    def test_call_control_records_failure_when_transfer_target_is_missing(self) -> None:
        client, events, tracker = self.build_client(asterisk=FakeAsterisk())

        response = client.post(
            "/calls/call-1/control",
            json={"action": "transfer", "response_to_event_id": 43},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn(43, tracker.responded_event_ids)
        persisted = events.list_events(call_id="call-1")
        self.assertEqual([event.type for event in persisted], ["call_control_requested", "call_control_completed"])
        self.assertFalse(persisted[1].data["ok"])
        self.assertEqual(persisted[1].data["message"], "transfer requires target")

    def test_call_control_records_successful_result(self) -> None:
        client, events, tracker = self.build_client(asterisk=FakeAsterisk())

        response = client.post(
            "/calls/call-1/control",
            json={"action": "transfer", "target": "123", "response_to_event_id": 44},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(44, tracker.responded_event_ids)
        persisted = events.list_events(call_id="call-1")
        self.assertEqual([event.type for event in persisted], ["call_control_requested", "call_control_completed"])
        self.assertTrue(persisted[1].data["ok"])
        self.assertEqual(persisted[1].data["message"], "transferred call-1 to 123")


if __name__ == "__main__":
    unittest.main()
