from __future__ import annotations

import tempfile
import unittest

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.calls import CallRegistry
from voicebot.config import Settings
from voicebot.events import EventStore
from voicebot.subagents import SubagentCoordinator, SubagentTask, SubagentTaskRequest, SubagentTaskResult, SubagentTaskStore
from voicebot.transcripts import TranscriptStore


class FakeProvider:
    kind = "internal_worker"

    def submit(self, request: SubagentTaskRequest) -> SubagentTask:
        task, _created = SubagentTaskStore().get_or_create_requested(request)
        return task.with_status("running", external_task_id="external-1", progress_message="Working on it.")

    def poll(self, task: SubagentTask) -> SubagentTask:
        return task.with_status(
            "completed",
            result=SubagentTaskResult(summary="The colleague found the answer.", content="There are 42 pages."),
        )

    def cancel(self, task: SubagentTask) -> SubagentTask:
        return task.with_status("cancelled")


class SubagentApiTests(unittest.TestCase):
    def build_client(self) -> tuple[TestClient, EventStore]:
        events = EventStore(max_context_events=50)
        coordinator = SubagentCoordinator(events=events)
        coordinator.register(FakeProvider())
        app = create_app(
            events,
            CallRegistry(),
            AgentTaskTracker(),
            WebSocketHub(),
            TranscriptStore(tempfile.mkdtemp()),
            None,
            settings=Settings(flowhunt_workspace_id="workspace-1"),
            subagent_coordinator=coordinator,
        )
        return TestClient(app), events

    def test_provider_neutral_subagent_api_submits_schedules_and_cancels_task(self) -> None:
        client, _events = self.build_client()

        response = client.post(
            "/subagent/tasks",
            json={
                "workspace_id": "workspace-1",
                "voicebot_id": "voicebot-1",
                "session_id": "session-1",
                "request_event_id": 7,
                "provider": "internal_worker",
                "input_text": "Check the sitemap.",
                "metadata": {},
            },
        )

        self.assertEqual(response.status_code, 200)
        task = response.json()["task"]
        self.assertEqual(task["workspace_id"], "workspace-1")
        self.assertEqual(task["provider"], "internal_worker")
        self.assertEqual(task["status"], "running")
        self.assertIsNotNone(task["next_poll_at"])

        catalog = client.get("/subagent/providers")
        self.assertTrue(catalog.json()["providers"]["internal_worker"]["registered"])

        cancelled = client.post(f"/subagent/tasks/{task['task_id']}/cancel", json={"workspace_id": "workspace-1"})
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json()["task"]["status"], "cancelled")
        self.assertTrue(cancelled.json()["task"]["terminal_event_emitted_at"])

    def test_speculative_subagent_api_starts_confirms_and_cancels(self) -> None:
        client, events = self.build_client()

        response = client.post(
            "/subagent/tasks/speculative",
            json={
                "workspace_id": "workspace-1",
                "voicebot_id": "voicebot-1",
                "session_id": "call-1",
                "request_event_id": 7,
                "provider": "internal_worker",
                "input_text": "Check sta",
                "speculative_key": "turn-1",
                "metadata": {},
            },
        )

        self.assertEqual(response.status_code, 200)
        task = response.json()["task"]
        self.assertTrue(task["metadata"]["speculative"])
        self.assertEqual(task["metadata"]["speculative_status"], "started")

        confirmed = client.post(
            f"/subagent/tasks/{task['task_id']}/confirm-speculative",
            json={
                "workspace_id": "workspace-1",
                "final_request_event_id": 8,
                "final_input_text": "Check status page.",
            },
        )
        self.assertEqual(confirmed.status_code, 200)
        self.assertEqual(confirmed.json()["task"]["metadata"]["speculative_status"], "confirmed")

        cancelled = client.post(
            f"/subagent/tasks/{task['task_id']}/cancel-speculative",
            json={"workspace_id": "workspace-1", "reason": "newer_turn"},
        )
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json()["task"]["metadata"]["speculative_status"], "cancelled")
        event_types = [event.type for event in events.list_events(call_id="call-1")]
        self.assertIn("subagent_task_speculative_started", event_types)
        self.assertIn("subagent_task_speculative_confirmed", event_types)
        self.assertIn("subagent_task_speculative_cancelled", event_types)

    def test_delegate_to_subagent_tool_uses_generic_provider(self) -> None:
        client, events = self.build_client()

        response = client.post(
            "/agent/tools/delegate_to_subagent",
            json={
                "arguments": {
                    "call_id": "call-1",
                    "provider": "internal_worker",
                    "message": "Count pages with a colleague.",
                    "response_to_event_id": 11,
                }
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["task"]["provider"], "internal_worker")
        self.assertEqual(response.json()["task"]["workspace_id"], "workspace-1")
        self.assertIn(11, client.get("/agent/tasks/status").json()["responded_event_ids"])
        event_types = [event.type for event in events.list_events(call_id="call-1")]
        self.assertIn("subagent_task_requested", event_types)
        self.assertIn("subagent_task_updated", event_types)


if __name__ == "__main__":
    unittest.main()
