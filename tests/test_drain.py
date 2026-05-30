from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.calls import CallRegistry
from voicebot.drain import DrainState, rollout_contract
from voicebot.events import EventStore
from voicebot.transcripts import TranscriptStore


class FakeSession:
    def __init__(self, call_id: str = "call-1") -> None:
        self.call_id = call_id
        self.stopped = False

    def snapshot(self) -> dict:
        return {
            "call_id": self.call_id,
            "route": {"workspace_id": "workspace-1", "voicebot_id": "voicebot-1"},
        }

    def stop(self) -> None:
        self.stopped = True


class DrainTests(unittest.TestCase):
    def test_drain_state_tracks_readiness_and_rollout_contract(self) -> None:
        state = DrainState()

        started = state.start("rollout")
        stopped = state.stop()
        contract = rollout_contract()

        self.assertTrue(started["draining"])
        self.assertFalse(started["readiness_accepts_new_sessions"])
        self.assertFalse(stopped["draining"])
        self.assertEqual(contract["failover_guarantee"]["active_rtp_or_webrtc_media"], "not transparently migrated; mark interrupted on owner loss")

    def test_drain_api_fails_readiness_but_not_liveness_and_interrupts_sessions(self) -> None:
        events = EventStore(max_context_events=50)
        registry = CallRegistry()
        session = FakeSession()
        registry.add(session)
        app = create_app(
            events,
            registry,
            AgentTaskTracker(),
            WebSocketHub(),
            TranscriptStore("/tmp/flowhunt-voicebot-test-transcripts"),
            None,
        )
        client = TestClient(app)

        start = client.post("/operations/drain/start", json={"reason": "rollout", "interrupt_active_sessions": True})
        readiness = client.get("/health/readiness")
        liveness = client.get("/health/liveness")
        state = client.get("/operations/drain")
        stop = client.post("/operations/drain/stop")

        self.assertEqual(start.status_code, 200)
        self.assertTrue(session.stopped)
        self.assertFalse(readiness.json()["ok"])
        self.assertTrue(liveness.json()["ok"])
        self.assertTrue(state.json()["drain"]["draining"])
        self.assertFalse(stop.json()["drain"]["draining"])
        event_types = [event.type for event in events.list_events()]
        self.assertIn("runtime_draining_started", event_types)
        self.assertIn("session_interrupted", event_types)
        self.assertIn("runtime_draining_stopped", event_types)


if __name__ == "__main__":
    unittest.main()
