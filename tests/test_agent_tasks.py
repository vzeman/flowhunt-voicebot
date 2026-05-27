from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
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

    def test_agent_task_claim_hides_claimed_events_until_expired(self) -> None:
        client, events, _tracker = self.build_client()
        first = events.append("call-1", "agent_response_requested", {"text": "first"})
        second = events.append("call-1", "agent_response_requested", {"text": "second"})

        claim_response = client.post(
            "/agent/tasks/claim",
            json={"event_ids": [first.id, second.id], "owner": "worker-1", "ttl_seconds": 30},
        )
        tasks_response = client.get("/agent/tasks?call_id=call-1")

        self.assertEqual(claim_response.status_code, 200)
        self.assertEqual(claim_response.json()["claimed_event_ids"], [first.id, second.id])
        self.assertEqual(tasks_response.status_code, 200)
        self.assertEqual(tasks_response.json()["pending"], [])
        claim_events = [
            event
            for event in events.list_events(call_id="call-1")
            if event.type == "agent_task_claimed"
        ]
        self.assertEqual([event.data["task_event_id"] for event in claim_events], [first.id, second.id])
        self.assertEqual([event.data["owner"] for event in claim_events], ["worker-1", "worker-1"])

    def test_agent_tasks_ignore_claim_events_when_listing_pending_tasks(self) -> None:
        client, events, _tracker = self.build_client()
        first = events.append("call-1", "agent_response_requested", {"text": "first"})
        client.post(
            "/agent/tasks/claim",
            json={"event_ids": [first.id], "owner": "worker-1", "ttl_seconds": 0.1},
        )

        import time

        time.sleep(0.12)
        response = client.get("/agent/tasks?call_id=call-1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual([event["id"] for event in response.json()["pending"]], [first.id])

    def test_agent_task_claim_skips_responded_events(self) -> None:
        client, events, tracker = self.build_client()
        first = events.append("call-1", "agent_response_requested", {"text": "first"})
        tracker.mark_responded(first.id)

        response = client.post(
            "/agent/tasks/claim",
            json={"event_ids": [first.id], "owner": "worker-1", "ttl_seconds": 30},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["claimed_event_ids"], [])

    def test_agent_task_claim_skips_missing_non_task_and_inactive_events(self) -> None:
        client, events, _tracker = self.build_client()
        non_task = events.append("call-1", "user_transcript", {"text": "not a task"})
        inactive_task = events.append("inactive", "agent_response_requested", {"text": "inactive"})

        response = client.post(
            "/agent/tasks/claim",
            json={"event_ids": [999999, non_task.id, inactive_task.id], "owner": "worker-1", "ttl_seconds": 30},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["claimed_event_ids"], [])
        self.assertEqual(events.list_events(call_id="call-1")[-1].type, "user_transcript")

    def test_agent_task_claim_expires(self) -> None:
        client, events, _tracker = self.build_client()
        first = events.append("call-1", "agent_response_requested", {"text": "first"})

        claim_response = client.post(
            "/agent/tasks/claim",
            json={"event_ids": [first.id], "owner": "worker-1", "ttl_seconds": 0.1},
        )
        self.assertEqual(claim_response.json()["claimed_event_ids"], [first.id])

        import time

        time.sleep(0.12)
        tasks_response = client.get("/agent/tasks?call_id=call-1")

        self.assertEqual([event["id"] for event in tasks_response.json()["pending"]], [first.id])

    def test_agent_task_release_makes_claimed_events_pending_again(self) -> None:
        client, events, _tracker = self.build_client()
        first = events.append("call-1", "agent_response_requested", {"text": "first"})

        claim_response = client.post(
            "/agent/tasks/claim",
            json={"event_ids": [first.id], "owner": "worker-1", "ttl_seconds": 30},
        )
        release_response = client.post("/agent/tasks/release", json={"event_ids": [first.id]})
        tasks_response = client.get("/agent/tasks?call_id=call-1")

        self.assertEqual(claim_response.json()["claimed_event_ids"], [first.id])
        self.assertEqual(release_response.status_code, 200)
        self.assertEqual(release_response.json()["released_event_ids"], [first.id])
        self.assertEqual([event["id"] for event in tasks_response.json()["pending"]], [first.id])
        release_events = [
            event
            for event in events.list_events(call_id="call-1")
            if event.type == "agent_task_released"
        ]
        self.assertEqual([event.data["task_event_id"] for event in release_events], [first.id])

    def test_agent_task_status_reports_claims_and_responded_events(self) -> None:
        client, events, tracker = self.build_client()
        first = events.append("call-1", "agent_response_requested", {"text": "first"})
        second = events.append("call-1", "agent_response_requested", {"text": "second"})
        tracker.mark_responded(first.id)
        client.post(
            "/agent/tasks/claim",
            json={"event_ids": [second.id], "owner": "worker-1", "ttl_seconds": 30},
        )

        response = client.get("/agent/tasks/status")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["responded_event_ids"], [first.id])
        self.assertEqual(payload["claims"][str(second.id)]["owner"], "worker-1")
        self.assertGreater(payload["claims"][str(second.id)]["expires_in_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
