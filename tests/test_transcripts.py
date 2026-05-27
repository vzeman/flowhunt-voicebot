from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.calls import CallRegistry
from voicebot.events import EventStore
from voicebot.transcripts import TranscriptStore, event_id


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
                        "skipped_line_count": 0,
                    }
                ],
            )

    def test_transcript_store_summaries_can_page_by_call_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcripts = TranscriptStore(directory)
            transcripts.append(FakeEvent(1, "call-1", "call_started", "2026-05-27T00:00:00Z", {}))
            transcripts.append(FakeEvent(2, "call-2", "call_started", "2026-05-27T00:00:01Z", {}))
            transcripts.append(FakeEvent(3, "call-3", "call_started", "2026-05-27T00:00:02Z", {}))

            summaries = transcripts.summaries(after_call_id="call-1", limit=1)

            self.assertEqual([summary["call_id"] for summary in summaries], ["call-2"])

    def test_transcript_store_read_can_page_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcripts = TranscriptStore(directory)
            transcripts.append(FakeEvent(1, "call-1", "call_started", "2026-05-27T00:00:00Z", {}))
            transcripts.append(FakeEvent(2, "call-1", "user_transcript", "2026-05-27T00:00:01Z", {}))
            transcripts.append(FakeEvent(3, "call-1", "call_ended", "2026-05-27T00:00:02Z", {}))

            events = transcripts.read("call-1", after=1, limit=1)

            self.assertEqual([event["id"] for event in events], [2])

    def test_transcript_store_skips_malformed_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "call-1.jsonl"
            path.write_text(
                '{"id":1,"call_id":"call-1","type":"call_started","timestamp":"2026-05-27T00:00:00Z","data":{}}\n'
                "not-json\n"
                '["not", "an", "event"]\n'
                '{"id":2,"call_id":"call-1","type":"call_ended","timestamp":"2026-05-27T00:00:01Z","data":{}}\n',
                encoding="utf-8",
            )
            transcripts = TranscriptStore(directory)

            events = transcripts.read("call-1")

            self.assertEqual([event["id"] for event in events], [1, 2])

    def test_transcript_summary_reports_skipped_line_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "call-1.jsonl"
            path.write_text(
                '{"id":1,"call_id":"call-1","type":"call_started","timestamp":"2026-05-27T00:00:00Z","data":{}}\n'
                "not-json\n"
                '["not", "an", "event"]\n',
                encoding="utf-8",
            )
            transcripts = TranscriptStore(directory)

            summaries = transcripts.summaries()

            self.assertEqual(summaries[0]["skipped_line_count"], 2)

    def test_transcript_summary_reports_fully_corrupt_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "call-1.jsonl"
            path.write_text("not-json\n[]\n", encoding="utf-8")
            transcripts = TranscriptStore(directory)

            summaries = transcripts.summaries()

            self.assertEqual(
                summaries,
                [
                    {
                        "call_id": "call-1",
                        "event_count": 0,
                        "first_event_id": None,
                        "last_event_id": None,
                        "first_timestamp": None,
                        "last_timestamp": None,
                        "skipped_line_count": 2,
                    }
                ],
            )

    def test_transcript_store_stats_reports_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcripts = TranscriptStore(directory)
            transcripts.append(FakeEvent(1, "call-1", "call_started", "2026-05-27T00:00:00Z", {}))
            path = Path(directory) / "call-2.jsonl"
            path.write_text(
                '{"id":2,"call_id":"call-2","type":"call_started","timestamp":"2026-05-27T00:00:00Z","data":{}}\n'
                "not-json\n",
                encoding="utf-8",
            )

            self.assertEqual(
                transcripts.stats(),
                {
                    "transcript_count": 2,
                    "event_count": 2,
                    "skipped_line_count": 1,
                    "corrupt_transcript_count": 1,
                    "corrupt_call_ids": ["call-2"],
                },
            )

    def test_transcript_store_ignores_invalid_event_ids_when_paging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "call-1.jsonl"
            path.write_text(
                '{"id":"bad","call_id":"call-1","type":"call_started","timestamp":"2026-05-27T00:00:00Z","data":{}}\n'
                '{"id":2,"call_id":"call-1","type":"call_ended","timestamp":"2026-05-27T00:00:01Z","data":{}}\n',
                encoding="utf-8",
            )
            transcripts = TranscriptStore(directory)

            events = transcripts.read("call-1", after=1)

            self.assertEqual([event["id"] for event in events], [2])

    def test_event_id_returns_zero_for_invalid_values(self) -> None:
        self.assertEqual(event_id({"id": "bad"}), 0)
        self.assertEqual(event_id({"id": None}), 0)
        self.assertEqual(event_id({"id": 3}), 3)

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

    def test_transcript_summary_endpoint_applies_pagination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcripts = TranscriptStore(directory)
            transcripts.append(FakeEvent(1, "call-1", "call_started", "2026-05-27T00:00:00Z", {}))
            transcripts.append(FakeEvent(2, "call-2", "call_started", "2026-05-27T00:00:01Z", {}))
            app = create_app(
                EventStore(max_context_events=20),
                CallRegistry(),
                AgentTaskTracker(),
                WebSocketHub(),
                transcripts,
                None,
            )
            client = TestClient(app)

            response = client.get("/transcripts/summary?after_call_id=call-1&limit=1")

            self.assertEqual(response.status_code, 200)
            self.assertEqual([item["call_id"] for item in response.json()["transcripts"]], ["call-2"])

    def test_call_transcript_endpoint_applies_pagination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcripts = TranscriptStore(directory)
            transcripts.append(FakeEvent(1, "call-1", "call_started", "2026-05-27T00:00:00Z", {}))
            transcripts.append(FakeEvent(2, "call-1", "user_transcript", "2026-05-27T00:00:01Z", {}))
            app = create_app(
                EventStore(max_context_events=20),
                CallRegistry(),
                AgentTaskTracker(),
                WebSocketHub(),
                transcripts,
                None,
            )
            client = TestClient(app)

            response = client.get("/calls/call-1/transcript?after=1&limit=1")

            self.assertEqual(response.status_code, 200)
            self.assertEqual([event["id"] for event in response.json()["events"]], [2])

    def test_call_transcript_endpoint_rejects_invalid_pagination(self) -> None:
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

            response = client.get("/calls/call-1/transcript?after=bad")

            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["detail"], "after must be an integer")

    def test_transcript_summary_endpoint_rejects_invalid_limit(self) -> None:
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

            response = client.get("/transcripts/summary?limit=bad")

            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["detail"], "limit must be an integer")

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

    def test_list_transcript_summaries_agent_tool_applies_pagination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcripts = TranscriptStore(directory)
            transcripts.append(FakeEvent(1, "call-1", "call_started", "2026-05-27T00:00:00Z", {}))
            transcripts.append(FakeEvent(2, "call-2", "call_started", "2026-05-27T00:00:01Z", {}))
            app = create_app(
                EventStore(max_context_events=20),
                CallRegistry(),
                AgentTaskTracker(),
                WebSocketHub(),
                transcripts,
                None,
            )
            client = TestClient(app)

            response = client.post(
                "/agent/tools/list_transcript_summaries",
                json={"arguments": {"after_call_id": "call-1", "limit": 1}},
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual([item["call_id"] for item in response.json()["transcripts"]], ["call-2"])

    def test_get_transcript_agent_tool_applies_pagination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcripts = TranscriptStore(directory)
            transcripts.append(FakeEvent(1, "call-1", "call_started", "2026-05-27T00:00:00Z", {}))
            transcripts.append(FakeEvent(2, "call-1", "user_transcript", "2026-05-27T00:00:01Z", {}))
            app = create_app(
                EventStore(max_context_events=20),
                CallRegistry(),
                AgentTaskTracker(),
                WebSocketHub(),
                transcripts,
                None,
            )
            client = TestClient(app)

            response = client.post(
                "/agent/tools/get_transcript",
                json={"arguments": {"call_id": "call-1", "after": 1, "limit": 1}},
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual([event["id"] for event in response.json()["events"]], [2])


if __name__ == "__main__":
    unittest.main()
