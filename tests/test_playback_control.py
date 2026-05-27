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


class PlaybackControlTests(unittest.TestCase):
    def make_session(self, call_id: str, events: EventStore) -> tuple[CallSession, socket.socket, socket.socket]:
        left, right = socket.socketpair()
        session = CallSession(call_id, left, Settings(), events, FakeSTT(), FakeTTS())
        return session, left, right

    def test_call_session_interrupt_playback_clears_buffer_and_records_event(self) -> None:
        events = EventStore(max_context_events=20)
        session, left, right = self.make_session("call-1", events)
        try:
            session.playback.enqueue(np.ones(80, dtype=np.float32))

            event = session.interrupt_playback("test")

            self.assertEqual(event.type, "bot_playback_interrupted")
            self.assertTrue(event.data["interrupted"])
            self.assertFalse(session.playback.is_active())
            self.assertEqual(events.list_events(call_id="call-1"), [event])
        finally:
            left.close()
            right.close()

    def test_stop_playback_tool_records_interrupt_event(self) -> None:
        events = EventStore(max_context_events=20)
        registry = CallRegistry()
        tracker = AgentTaskTracker()
        session, left, right = self.make_session("call-1", events)
        session.playback.enqueue(np.ones(80, dtype=np.float32))
        registry.add(session)
        try:
            app = create_app(
                events,
                registry,
                tracker,
                WebSocketHub(),
                TranscriptStore("/tmp/flowhunt-voicebot-test-transcripts"),
                None,
            )
            client = TestClient(app)

            response = client.post(
                "/agent/tools/stop_playback",
                json={
                    "arguments": {
                        "call_id": "call-1",
                        "reason": "agent_test",
                        "response_to_event_id": 123,
                    }
                },
            )

            self.assertEqual(response.status_code, 200)
            self.assertIn(123, tracker.responded_event_ids)
            event = response.json()["event"]
            self.assertEqual(event["type"], "bot_playback_interrupted")
            self.assertEqual(event["data"]["reason"], "agent_test")
            self.assertTrue(event["data"]["interrupted"])
        finally:
            left.close()
            right.close()


if __name__ == "__main__":
    unittest.main()
