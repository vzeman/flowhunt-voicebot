from __future__ import annotations

import socket
import threading
import time
import unittest

import numpy as np
from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.calls import AgentResponse, CallRegistry, CallSession
from voicebot.config import Settings
from voicebot.events import EventStore
from voicebot.transcripts import TranscriptStore
from voicebot.webrtc import WebRTCCallSession


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

    def test_startup_response_survives_startup_vad_until_recording_clears(self) -> None:
        events = EventStore(max_context_events=20)
        session = WebRTCCallSession(
            "call-1",
            "session-1",
            Settings(),
            events,
            FakeSTT(),
            FakeTTS(),
        )
        try:
            request = events.append("call-1", "agent_response_requested", {"reason": "call_connected"})
            session._remember_response_generation(request.id)
            session._protect_startup_response(request.id)
            session.recording_event.set()
            session._mark_interrupted("startup_noise")

            session.submit_agent_response(
                AgentResponse("call-1", "Hello, how can I help?", response_to_event_id=request.id)
            )
            silent_packet, started, finished = session.next_playback_packet(80)

            self.assertFalse(started)
            self.assertFalse(finished)
            self.assertTrue(session.playback.is_active())
            self.assertEqual(float(np.max(np.abs(silent_packet))), 0.0)
            self.assertNotIn(
                "agent_response_dropped",
                [event.type for event in events.list_events(call_id="call-1")],
            )

            session.recording_event.clear()
            packet, started, _finished = session.next_playback_packet(80)

            self.assertTrue(started)
            self.assertEqual(len(packet), 80)
        finally:
            session.stop()

    def test_colleague_result_waits_for_current_speech_turn(self) -> None:
        events = EventStore(max_context_events=30)
        session = WebRTCCallSession(
            "call-1",
            "session-1",
            Settings(deferred_response_wait_seconds=1.0),
            events,
            FakeSTT(),
            FakeTTS(),
        )
        try:
            request = events.append("call-1", "agent_response_requested", {"reason": "colleague_result"})
            session.recording_event.set()

            def clear_recording() -> None:
                time.sleep(0.05)
                session.recording_event.clear()

            threading.Thread(target=clear_recording, daemon=True).start()

            session.submit_agent_response(
                AgentResponse("call-1", "The answer is 1,950 pages.", response_to_event_id=request.id)
            )

            event_types = [event.type for event in events.list_events(call_id="call-1")]
            self.assertIn("agent_response_deferred", event_types)
            self.assertIn("agent_response_queued", event_types)
            self.assertNotIn("agent_response_dropped", event_types)
            self.assertTrue(session.playback.is_active())
        finally:
            session.stop()


if __name__ == "__main__":
    unittest.main()
