from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.calls import CallRegistry
from voicebot.events import EventStore
from voicebot.transcripts import TranscriptStore


class FailingAccessLogEventStore(EventStore):
    def append(self, call_id: str, event_type, data=None):  # type: ignore[override]
        if event_type == "api_access_logged":
            raise OSError("access log unavailable")
        return super().append(call_id, event_type, data)


class AccessLoggingTests(unittest.TestCase):
    def test_access_log_failure_does_not_break_health(self) -> None:
        app = create_app(
            FailingAccessLogEventStore(max_context_events=20),
            CallRegistry(),
            AgentTaskTracker(),
            WebSocketHub(),
            TranscriptStore("/tmp/flowhunt-voicebot-test-transcripts"),
            None,
        )

        response = TestClient(app).get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])


if __name__ == "__main__":
    unittest.main()
