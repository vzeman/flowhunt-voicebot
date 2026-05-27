from __future__ import annotations

from dataclasses import dataclass
import tempfile
import unittest

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.calls import CallRegistry
from voicebot.events import EventStore
from voicebot.transcripts import TranscriptStore


@dataclass
class FakeEvent:
    id: int
    call_id: str
    type: str
    timestamp: str
    data: dict


class TranscriptTests(unittest.TestCase):
    def test_transcript_store_lists_persisted_call_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcripts = TranscriptStore(directory)
            transcripts.append(FakeEvent(1, "call-2", "call_started", "2026-05-27T00:00:00Z", {}))
            transcripts.append(FakeEvent(2, "call-1", "call_started", "2026-05-27T00:00:00Z", {}))

            self.assertEqual(transcripts.list_call_ids(), ["call-1", "call-2"])

    def test_transcript_store_summarizes_persisted_transcripts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcripts = TranscriptStore(directory)
            transcripts.append(FakeEvent(1, "call-1", "call_started", "2026-05-27T00:00:00Z", {}))
            transcripts.append(FakeEvent(2, "call-1", "call_ended", "2026-05-27T00:00:03Z", {}))

            self.assertEqual(
                transcripts.summaries(),
                [
                    {
                        "call_id": "call-1",
                        "event_count": 2,
                        "first_event_id": 1,
                        "last_event_id": 2,
                        "first_timestamp": "2026-05-27T00:00:00Z",
                        "last_timestamp": "2026-05-27T00:00:03Z",
                    }
                ],
            )

    def test_transcripts_endpoint_lists_persisted_call_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcripts = TranscriptStore(directory)
            transcripts.append(FakeEvent(1, "call-1", "call_started", "2026-05-27T00:00:00Z", {}))
            app = create_app(
                EventStore(max_context_events=20),
                CallRegistry(),
                AgentTaskTracker(),
                WebSocketHub(),
                transcripts,
                None,
            )
            client = TestClient(app)

            response = client.get("/transcripts")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"call_ids": ["call-1"]})

    def test_transcript_summary_endpoint_lists_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcripts = TranscriptStore(directory)
            transcripts.append(FakeEvent(1, "call-1", "call_started", "2026-05-27T00:00:00Z", {}))
            app = create_app(
                EventStore(max_context_events=20),
                CallRegistry(),
                AgentTaskTracker(),
                WebSocketHub(),
                transcripts,
                None,
            )
            client = TestClient(app)

            response = client.get("/transcripts/summary")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["transcripts"][0]["call_id"], "call-1")
            self.assertEqual(response.json()["transcripts"][0]["event_count"], 1)

    def test_list_transcripts_agent_tool_lists_persisted_call_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcripts = TranscriptStore(directory)
            transcripts.append(FakeEvent(1, "call-1", "call_started", "2026-05-27T00:00:00Z", {}))
            app = create_app(
                EventStore(max_context_events=20),
                CallRegistry(),
                AgentTaskTracker(),
                WebSocketHub(),
                transcripts,
                None,
            )
            client = TestClient(app)

            response = client.post("/agent/tools/list_transcripts", json={"arguments": {}})

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"call_ids": ["call-1"]})

    def test_list_transcript_summaries_agent_tool_lists_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcripts = TranscriptStore(directory)
            transcripts.append(FakeEvent(1, "call-1", "call_started", "2026-05-27T00:00:00Z", {}))
            app = create_app(
                EventStore(max_context_events=20),
                CallRegistry(),
                AgentTaskTracker(),
                WebSocketHub(),
                transcripts,
                None,
            )
            client = TestClient(app)

            response = client.post("/agent/tools/list_transcript_summaries", json={"arguments": {}})

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["transcripts"][0]["call_id"], "call-1")
            self.assertEqual(response.json()["transcripts"][0]["event_count"], 1)


if __name__ == "__main__":
    unittest.main()
