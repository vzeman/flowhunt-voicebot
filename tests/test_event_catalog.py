from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.calls import CallRegistry
from voicebot.event_catalog import event_catalog, missing_catalog_event_types
from voicebot.events import EventStore
from voicebot.transcripts import TranscriptStore


class EventCatalogTests(unittest.TestCase):
    def test_event_catalog_covers_all_declared_event_types(self) -> None:
        self.assertEqual(missing_catalog_event_types(), set())

    def test_event_catalog_entries_have_required_fields(self) -> None:
        for entry in event_catalog():
            self.assertIsInstance(entry["type"], str)
            self.assertIsInstance(entry["category"], str)
            self.assertIsInstance(entry["description"], str)
            self.assertIsInstance(entry["agent_visible"], bool)

    def test_event_catalog_endpoint_returns_catalog(self) -> None:
        app = create_app(
            EventStore(max_context_events=20),
            CallRegistry(),
            AgentTaskTracker(),
            WebSocketHub(),
            TranscriptStore("/tmp/flowhunt-voicebot-test-transcripts"),
            None,
        )
        client = TestClient(app)

        response = client.get("/events/catalog")

        self.assertEqual(response.status_code, 200)
        event_types = {entry["type"] for entry in response.json()["events"]}
        self.assertIn("call_connected", event_types)
        self.assertIn("agent_response_requested", event_types)
        self.assertIn("call_control_completed", event_types)


if __name__ == "__main__":
    unittest.main()
