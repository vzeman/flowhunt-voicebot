from __future__ import annotations

import socket
import unittest

import numpy as np
from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.calls import CallRegistry, CallSession
from voicebot.config import Settings
from voicebot.events import EventStore
from voicebot.transcripts import TranscriptStore


class FakeSTT:
    def transcribe(self, call_audio):
        raise AssertionError("not used")

    def transcribe_stream(self, call_audio):
        raise AssertionError("not used")


class FakeTTS:
    def synthesize(self, text: str):
        return np.zeros(80, dtype=np.float32), 0.01

    def synthesize_stream(self, text: str):
        yield self.synthesize(text)


class CallStateTests(unittest.TestCase):
    def make_session(self, call_id: str) -> tuple[CallSession, socket.socket, socket.socket]:
        left, right = socket.socketpair()
        session = CallSession(
            call_id,
            left,
            Settings(),
            EventStore(max_context_events=20),
            FakeSTT(),
            FakeTTS(),
        )
        return session, left, right

    def test_call_session_snapshot_reports_runtime_state(self) -> None:
        session, left, right = self.make_session("call-1")
        try:
            session.recording_event.set()

            snapshot = session.snapshot()

            self.assertEqual(snapshot["call_id"], "call-1")
            self.assertTrue(snapshot["recording"])
            self.assertFalse(snapshot["playback_active"])
            self.assertFalse(snapshot["stopped"])
            self.assertEqual(snapshot["active_turn"], 0)
            self.assertEqual(snapshot["transport"], "asterisk_audiosocket")
            self.assertIn("transfer", snapshot["capabilities"]["call_control"])
        finally:
            left.close()
            right.close()

    def test_call_registry_returns_sorted_snapshots(self) -> None:
        registry = CallRegistry()
        session_b, left_b, right_b = self.make_session("call-b")
        session_a, left_a, right_a = self.make_session("call-a")
        try:
            registry.add(session_b)
            registry.add(session_a)

            self.assertEqual([item["call_id"] for item in registry.snapshots()], ["call-a", "call-b"])
            self.assertEqual(registry.snapshot("call-a")["call_id"], "call-a")
            self.assertIsNone(registry.snapshot("missing"))
        finally:
            left_a.close()
            right_a.close()
            left_b.close()
            right_b.close()

    def test_call_state_api_and_tool_return_active_call_state(self) -> None:
        registry = CallRegistry()
        session, left, right = self.make_session("call-1")
        registry.add(session)
        try:
            app = create_app(
                EventStore(max_context_events=20),
                registry,
                AgentTaskTracker(),
                WebSocketHub(),
                TranscriptStore("/tmp/flowhunt-voicebot-test-transcripts"),
                None,
            )
            client = TestClient(app)

            list_response = client.get("/calls")
            state_response = client.get("/calls/call-1")
            tool_response = client.post("/agent/tools/get_call_state", json={"arguments": {"call_id": "call-1"}})

            self.assertEqual(list_response.status_code, 200)
            self.assertEqual([item["call_id"] for item in list_response.json()["calls"]], ["call-1"])
            self.assertEqual(state_response.status_code, 200)
            self.assertEqual(state_response.json()["call_id"], "call-1")
            self.assertEqual(tool_response.status_code, 200)
            self.assertEqual(tool_response.json()["call_id"], "call-1")
        finally:
            left.close()
            right.close()


if __name__ == "__main__":
    unittest.main()
