from __future__ import annotations

import tempfile
import unittest

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.calls import CallRegistry
from voicebot.config import Settings
from voicebot.events import EventStore
from voicebot.transcripts import TranscriptStore


class RuntimeConfigApiTests(unittest.TestCase):
    def build_client(self) -> TestClient:
        self.directory = tempfile.TemporaryDirectory()
        settings = Settings(openai_api_key="configured-openai", ami_password="configured-ami")
        app = create_app(
            EventStore(max_context_events=20),
            CallRegistry(),
            AgentTaskTracker(),
            WebSocketHub(),
            TranscriptStore(self.directory.name),
            None,
            settings,
        )
        return TestClient(app)

    def tearDown(self) -> None:
        directory = getattr(self, "directory", None)
        if directory is not None:
            directory.cleanup()

    def test_config_endpoint_returns_redacted_settings(self) -> None:
        client = self.build_client()

        response = client.get("/config")

        self.assertEqual(response.status_code, 200)
        settings = response.json()["settings"]
        self.assertEqual(settings["openai_api_key"], {"configured": True, "redacted": True})
        self.assertEqual(settings["ami_password"], {"configured": True, "redacted": True})
        self.assertNotIn("configured-openai", str(settings))
        self.assertNotIn("configured-ami", str(settings))

    def test_agent_tool_returns_redacted_runtime_config(self) -> None:
        client = self.build_client()

        response = client.post("/agent/tools/get_runtime_config", json={"arguments": {}})

        self.assertEqual(response.status_code, 200)
        settings = response.json()["settings"]
        self.assertEqual(settings["openai_api_key"], {"configured": True, "redacted": True})
        self.assertEqual(settings["ami_password"], {"configured": True, "redacted": True})

    def test_tool_schema_includes_runtime_config_tool(self) -> None:
        client = self.build_client()

        response = client.get("/agent/tools/schema")

        self.assertEqual(response.status_code, 200)
        tool_names = {tool["name"] for tool in response.json()["tools"]}
        self.assertIn("get_runtime_config", tool_names)


if __name__ == "__main__":
    unittest.main()
