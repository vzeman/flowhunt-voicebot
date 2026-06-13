from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.calls import CallRegistry
from voicebot.events import EventStore
from voicebot.metrics import summarize_metrics
from voicebot.transcripts import TranscriptStore


class MetricsTests(unittest.TestCase):
    def test_summarize_metrics_groups_numeric_metric_events(self) -> None:
        events = EventStore(max_context_events=20)
        events.append("call-1", "metrics", {"name": "stt_duration_seconds", "value": 0.2, "provider": "openai"})
        latest = events.append("call-1", "metrics", {"name": "stt_duration_seconds", "value": 0.4, "provider": "openai"})
        events.append("call-1", "metrics", {"name": "ignored", "value": "not-number"})

        summary = summarize_metrics(events.list_events(call_id="call-1"))

        metric = summary["metrics"]["stt_duration_seconds"]
        self.assertEqual(metric["count"], 2)
        self.assertEqual(metric["min"], 0.2)
        self.assertEqual(metric["max"], 0.4)
        self.assertEqual(metric["avg"], 0.30000000000000004)
        self.assertEqual(metric["p50"], 0.30000000000000004)
        self.assertEqual(metric["p90"], 0.38)
        self.assertEqual(metric["latest"]["event_id"], latest.id)
        self.assertEqual(summary["providers"]["openai"]["latency_count"], 2)

    def test_metrics_endpoint_filters_by_call_id(self) -> None:
        events = EventStore(max_context_events=20)
        events.append("call-1", "metrics", {"name": "tts_duration_seconds", "value": 0.5})
        events.append("call-2", "metrics", {"name": "tts_duration_seconds", "value": 1.5})
        app = create_app(
            events,
            CallRegistry(),
            AgentTaskTracker(),
            WebSocketHub(),
            TranscriptStore("/tmp/flowhunt-voicebot-test-transcripts"),
            None,
        )
        client = TestClient(app)

        response = client.get("/metrics?call_id=call-1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["metrics"]["tts_duration_seconds"]["avg"], 0.5)

    def test_get_metrics_agent_tool_returns_summary(self) -> None:
        events = EventStore(max_context_events=20)
        events.append("call-1", "metrics", {"name": "agent_response_latency_seconds", "value": 0.7})
        app = create_app(
            events,
            CallRegistry(),
            AgentTaskTracker(),
            WebSocketHub(),
            TranscriptStore("/tmp/flowhunt-voicebot-test-transcripts"),
            None,
        )
        client = TestClient(app)

        response = client.post("/agent/tools/get_metrics", json={"arguments": {"call_id": "call-1"}})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["metrics"]["agent_response_latency_seconds"]["latest"]["value"], 0.7)


if __name__ == "__main__":
    unittest.main()
