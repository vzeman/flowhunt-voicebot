from __future__ import annotations

import tempfile
import unittest

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.calls import CallRegistry
from voicebot.events import EventStore
from voicebot.subagents import SubagentCoordinator, SubagentTaskRequest, SubagentTaskResult
from voicebot.transcripts import TranscriptStore


class VoicebotTaskApiTests(unittest.TestCase):
    def build_client(self, coordinator: SubagentCoordinator | None = None) -> TestClient:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        app = create_app(
            EventStore(max_context_events=20),
            CallRegistry(),
            AgentTaskTracker(),
            WebSocketHub(),
            TranscriptStore(directory.name),
            None,
            subagent_coordinator=coordinator,
        )
        return TestClient(app)

    def test_workspace_task_api_lists_voicebot_tasks_with_filters(self) -> None:
        coordinator = SubagentCoordinator()
        first, _created = coordinator.store.get_or_create_requested(
            SubagentTaskRequest(
                workspace_id="workspace-1",
                voicebot_id="voicebot-1",
                session_id="session-1",
                request_event_id=1,
                provider="flowhunt_flow",
                input_text="count pages",
            )
        )
        coordinator.store.update(first.with_status("completed", result=SubagentTaskResult(summary="42 pages")))
        second, _created = coordinator.store.get_or_create_requested(
            SubagentTaskRequest(
                workspace_id="workspace-1",
                voicebot_id="voicebot-2",
                session_id="session-2",
                request_event_id=2,
                provider="flowhunt_flow",
                input_text="hidden",
            )
        )
        coordinator.store.update(second.with_status("running"))
        client = self.build_client(coordinator)

        listed = client.get("/workspaces/workspace-1/voicebots/voicebot-1/tasks")
        completed = client.get("/workspaces/workspace-1/voicebots/voicebot-1/tasks?status=completed")
        hidden = client.get("/workspaces/workspace-1/voicebots/voicebot-1/tasks?session_id=session-2")

        self.assertEqual([task["task_id"] for task in listed.json()["tasks"]], [first.task_id])
        self.assertEqual(completed.json()["tasks"][0]["result"]["summary"], "42 pages")
        self.assertEqual(hidden.json()["tasks"], [])

    def test_workspace_task_api_returns_unavailable_without_coordinator(self) -> None:
        client = self.build_client()

        response = client.get("/workspaces/workspace-1/voicebots/voicebot-1/tasks")

        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
