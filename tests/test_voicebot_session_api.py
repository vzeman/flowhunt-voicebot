from __future__ import annotations

import tempfile
import unittest

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.calls import CallRegistry
from voicebot.events import EventStore
from voicebot.execution_model import ExecutionScope
from voicebot.transcripts import TranscriptStore
from voicebot.workspace_model import VoicebotSessionRecord, VoicebotSessionStore


class VoicebotSessionApiTests(unittest.TestCase):
    def build_client(self) -> tuple[TestClient, EventStore, VoicebotSessionStore]:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        transcripts = TranscriptStore(directory.name)
        events = EventStore(max_context_events=20, transcript_store=transcripts)
        sessions = VoicebotSessionStore()
        app = create_app(
            events,
            CallRegistry(),
            AgentTaskTracker(),
            WebSocketHub(),
            transcripts,
            None,
            voicebot_sessions=sessions,
        )
        return TestClient(app), events, sessions

    def test_workspace_session_api_lists_reads_timeline_and_transcript(self) -> None:
        client, events, sessions = self.build_client()
        sessions.save(VoicebotSessionRecord("session-1", "workspace-1", "voicebot-1", channel_id="channel-1"))
        sessions.save(VoicebotSessionRecord("session-2", "workspace-1", "voicebot-2", status="active"))
        events.append_scoped(
            ExecutionScope("workspace-1", "voicebot-1", "session-1"),
            "user_transcript",
            {"text": "hello"},
        )
        events.append_scoped(
            ExecutionScope("workspace-1", "voicebot-2", "session-2"),
            "user_transcript",
            {"text": "hidden"},
        )

        listed = client.get("/workspaces/workspace-1/voicebots/voicebot-1/sessions")
        read = client.get("/workspaces/workspace-1/voicebots/voicebot-1/sessions/session-1")
        timeline = client.get("/workspaces/workspace-1/voicebots/voicebot-1/sessions/session-1/timeline")
        transcript = client.get("/workspaces/workspace-1/voicebots/voicebot-1/sessions/session-1/transcript")
        hidden = client.get("/workspaces/workspace-1/voicebots/voicebot-1/sessions/session-2")

        self.assertEqual([item["session_id"] for item in listed.json()["sessions"]], ["session-1"])
        self.assertEqual(read.json()["session"]["channel_id"], "channel-1")
        self.assertEqual([event["data"]["text"] for event in timeline.json()["events"]], ["hello"])
        self.assertEqual([event["data"]["text"] for event in transcript.json()["events"]], ["hello"])
        self.assertEqual(hidden.status_code, 404)

    def test_workspace_session_api_supports_active_filter_and_limit_validation(self) -> None:
        client, _events, sessions = self.build_client()
        sessions.save(VoicebotSessionRecord("active", "workspace-1", "voicebot-1"))
        sessions.save(VoicebotSessionRecord("ended", "workspace-1", "voicebot-1").end())

        active = client.get("/workspaces/workspace-1/voicebots/voicebot-1/sessions?active_only=true")
        invalid_limit = client.get("/workspaces/workspace-1/voicebots/voicebot-1/sessions/active/timeline?limit=0")

        self.assertEqual([item["session_id"] for item in active.json()["sessions"]], ["active"])
        self.assertEqual(invalid_limit.status_code, 400)


if __name__ == "__main__":
    unittest.main()
