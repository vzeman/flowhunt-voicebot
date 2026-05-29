from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.api_models import AgentToolRequest
from voicebot.calls import CallRegistry
from voicebot.events import EventStore
from voicebot.transcripts import TranscriptStore


class ApiLimitTests(unittest.TestCase):
    def build_client(self) -> tuple[TestClient, EventStore]:
        events = EventStore(max_context_events=20)
        app = create_app(
            events,
            CallRegistry(),
            AgentTaskTracker(),
            WebSocketHub(),
            TranscriptStore("/tmp/flowhunt-voicebot-test-transcripts"),
            None,
        )
        return TestClient(app), events

    def test_events_rejects_zero_limit(self) -> None:
        client, _events = self.build_client()

        response = client.get("/events?limit=0")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "limit must be at least 1")

    def test_events_rejects_oversized_limit(self) -> None:
        client, _events = self.build_client()

        response = client.get("/events?limit=1001")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "limit must be at most 1000")

    def test_get_events_tool_rejects_invalid_limit(self) -> None:
        client, _events = self.build_client()

        response = client.post("/agent/tools/get_events", json={"arguments": {"limit": 0}})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "limit must be at least 1")

    def test_get_events_tool_rejects_non_integer_after(self) -> None:
        client, _events = self.build_client()

        response = client.post("/agent/tools/get_events", json={"arguments": {"after": "abc"}})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "after must be an integer")

    def test_get_events_tool_rejects_non_integer_limit(self) -> None:
        client, _events = self.build_client()

        response = client.post("/agent/tools/get_events", json={"arguments": {"limit": "abc"}})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "limit must be an integer")

    def test_agent_tool_request_uses_independent_default_arguments(self) -> None:
        first = AgentToolRequest()
        second = AgentToolRequest()

        first.arguments["call_id"] = "call-1"

        self.assertEqual(second.arguments, {})


if __name__ == "__main__":
    unittest.main()
