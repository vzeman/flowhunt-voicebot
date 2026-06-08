from __future__ import annotations

import tempfile
import unittest

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.calls import CallRegistry
from voicebot.events import EventStore
from voicebot.transcripts import TranscriptStore


class AgentToolsApiTests(unittest.TestCase):
    def build_client(self) -> TestClient:
        app = create_app(
            EventStore(max_context_events=50),
            CallRegistry(),
            AgentTaskTracker(),
            WebSocketHub(),
            TranscriptStore(tempfile.mkdtemp()),
            None,
        )
        return TestClient(app)

    def test_agent_tool_catalogs_are_exposed(self) -> None:
        client = self.build_client()

        legacy = client.get("/agent/tools")
        schema = client.get("/agent/tools/schema")

        self.assertEqual(legacy.status_code, 200)
        self.assertIn("say", {tool["name"] for tool in legacy.json()["tools"]})
        self.assertEqual(schema.status_code, 200)
        self.assertIn("say", {tool["name"] for tool in schema.json()["tools"]})

    def test_unknown_agent_tool_returns_not_found(self) -> None:
        client = self.build_client()

        response = client.post("/agent/tools/not_registered", json={"arguments": {}})

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "unknown agent tool: not_registered")


if __name__ == "__main__":
    unittest.main()
