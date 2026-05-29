from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.calls import CallRegistry
from voicebot.events import EventStore
from voicebot.transcripts import TranscriptStore
from voicebot.workspace_model import VoicebotDefinition, VoicebotStore


class VoicebotAdminTests(unittest.TestCase):
    def build_client(self) -> tuple[TestClient, VoicebotStore]:
        store = VoicebotStore()
        app = create_app(
            EventStore(max_context_events=20),
            CallRegistry(),
            AgentTaskTracker(),
            WebSocketHub(),
            TranscriptStore("/tmp/flowhunt-voicebot-test-transcripts"),
            None,
            voicebots=store,
        )
        return TestClient(app), store

    def test_voicebot_admin_crud_is_workspace_scoped(self) -> None:
        client, _store = self.build_client()

        created = client.post(
            "/workspaces/workspace-1/voicebots",
            json={
                "voicebot_id": "voicebot-1",
                "display_name": "Support bot",
                "metadata": {"language": "en"},
            },
        )
        listed = client.get("/workspaces/workspace-1/voicebots")
        hidden = client.get("/workspaces/workspace-2/voicebots")
        read = client.get("/workspaces/workspace-1/voicebots/voicebot-1")
        patched = client.patch(
            "/workspaces/workspace-1/voicebots/voicebot-1",
            json={"enabled": False, "display_name": "Support bot v2"},
        )
        deleted = client.delete("/workspaces/workspace-1/voicebots/voicebot-1")
        missing = client.get("/workspaces/workspace-1/voicebots/voicebot-1")

        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["voicebot"]["workspace_id"], "workspace-1")
        self.assertEqual([item["voicebot_id"] for item in listed.json()["voicebots"]], ["voicebot-1"])
        self.assertEqual(hidden.json()["voicebots"], [])
        self.assertEqual(read.json()["voicebot"]["metadata"], {"language": "en"})
        self.assertFalse(patched.json()["voicebot"]["enabled"])
        self.assertEqual(patched.json()["voicebot"]["display_name"], "Support bot v2")
        self.assertTrue(deleted.json()["deleted"])
        self.assertEqual(missing.status_code, 404)

    def test_voicebot_admin_rejects_duplicates_and_invalid_records(self) -> None:
        client, store = self.build_client()
        store.create(VoicebotDefinition("workspace-1", "voicebot-1"))

        duplicate = client.post("/workspaces/workspace-1/voicebots", json={"voicebot_id": "voicebot-1"})
        invalid = client.post(
            "/workspaces/workspace-1/voicebots",
            json={"voicebot_id": " ", "display_name": "Invalid"},
        )
        missing_patch = client.patch("/workspaces/workspace-1/voicebots/missing", json={"enabled": False})

        self.assertEqual(duplicate.status_code, 400)
        self.assertIn("already exists", duplicate.json()["detail"])
        self.assertEqual(invalid.status_code, 400)
        self.assertIn("voicebot_id", invalid.json()["detail"])
        self.assertEqual(missing_patch.status_code, 404)


if __name__ == "__main__":
    unittest.main()
