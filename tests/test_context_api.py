from __future__ import annotations

import tempfile
import unittest

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.calls import CallRegistry
from voicebot.events import EventStore
from voicebot.transcripts import TranscriptStore


class ContextApiTests(unittest.TestCase):
    def build_client(self) -> tuple[TestClient, EventStore]:
        events = EventStore(max_context_events=5)
        app = create_app(
            events,
            CallRegistry(),
            AgentTaskTracker(),
            WebSocketHub(),
            TranscriptStore(tempfile.mkdtemp()),
            None,
        )
        return TestClient(app), events

    def test_context_endpoint_returns_call_scoped_event_context(self) -> None:
        client, events = self.build_client()
        events.append("call-1", "call_started", {"transport": "webrtc"})
        events.append("call-2", "call_started", {"transport": "sip"})

        response = client.get("/context?call_id=call-1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual([event["call_id"] for event in response.json()["events"]], ["call-1"])

    def test_context_compaction_replaces_summary(self) -> None:
        client, events = self.build_client()

        response = client.post(
            "/context/compact",
            json={"call_id": "call-1", "summary": "Caller asked about pricing."},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()["event"]
        self.assertEqual(payload["type"], "context_compacted")
        self.assertEqual(payload["call_id"], "call-1")
        self.assertEqual(events.context(call_id="call-1")["summary"], "Caller asked about pricing.")


if __name__ == "__main__":
    unittest.main()
