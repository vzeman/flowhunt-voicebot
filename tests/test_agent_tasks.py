from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from voicebot.api import AgentTaskTracker, WebSocketHub, create_app
from voicebot.calls import CallRegistry
from voicebot.events import EventStore
from voicebot.transcripts import TranscriptStore


class FakeCallRegistry(CallRegistry):
    def __init__(self, active_call_ids: list[str]) -> None:
        super().__init__()
        self._active_call_ids = active_call_ids

    def active_call_ids(self) -> list[str]:
        return self._active_call_ids


class AgentTasksTests(unittest.TestCase):
    def build_client(self) -> tuple[TestClient, EventStore, AgentTaskTracker]:
        events = EventStore(max_context_events=20)
        tracker = AgentTaskTracker()
        app = create_app(
            events,
            FakeCallRegistry(["call-1", "call-2"]),
            tracker,
            WebSocketHub(),
            TranscriptStore("/tmp/flowhunt-voicebot-test-transcripts"),
            None,
        )
        return TestClient(app), events, tracker

    def test_agent_tasks_filters_pending_events_by_call_id(self) -> None:
        client, events, tracker = self.build_client()
        first = events.append("call-1", "agent_response_requested", {"text": "hello"})
        events.append("call-2", "agent_response_requested", {"text": "other"})
        events.append("inactive", "agent_response_requested", {"text": "ignored"})

        response = client.get("/agent/tasks?call_id=call-1")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual([event["id"] for event in payload["pending"]], [first.id])
        self.assertEqual([event["call_id"] for event in payload["context"]["events"]], ["call-1"])
        self.assertEqual(tracker.responded_event_ids, set())

    def test_agent_tasks_applies_limit(self) -> None:
        client, events, _tracker = self.build_client()
        first = events.append("call-1", "agent_response_requested", {"text": "first"})
        events.append("call-1", "agent_response_requested", {"text": "second"})

        response = client.get("/agent/tasks?call_id=call-1&limit=1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual([event["id"] for event in response.json()["pending"]], [first.id])

    def test_agent_tasks_omits_responded_events(self) -> None:
        client, events, tracker = self.build_client()
        first = events.append("call-1", "agent_response_requested", {"text": "first"})
        second = events.append("call-1", "agent_response_requested", {"text": "second"})
        tracker.mark_responded(first.id)

        response = client.get("/agent/tasks?call_id=call-1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual([event["id"] for event in response.json()["pending"]], [second.id])


if __name__ == "__main__":
    unittest.main()
