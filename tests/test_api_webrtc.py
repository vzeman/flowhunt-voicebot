from __future__ import annotations

import tempfile
import unittest

import numpy as np
from fastapi.testclient import TestClient

from voicebot.audio import STT_SAMPLE_RATE
from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.calls import CallRegistry
from voicebot.events import EventStore
from voicebot.transcripts import TranscriptStore
from voicebot.webrtc import audio_frame_to_call_audio


class FakeWebRTCManager:
    def __init__(self) -> None:
        self.created = []
        self.closed = []

    def snapshots(self):
        return [{"session_id": "session-1", "call_id": "webrtc-session-1", "transport": "webrtc"}]

    async def create_session(self, sdp: str, offer_type: str, metadata: dict):
        self.created.append((sdp, offer_type, metadata))
        return {
            "session_id": "session-1",
            "call_id": "webrtc-session-1",
            "answer": {"sdp": "answer-sdp", "type": "answer"},
        }

    async def close_session(self, session_id: str) -> bool:
        self.closed.append(session_id)
        return session_id == "session-1"


class FakeAudioFrame:
    sample_rate = 48000

    def to_ndarray(self):
        return np.ones((1, 480), dtype=np.float32) * 0.25


class FakePackedStereoAudioFrame:
    sample_rate = 48000

    def to_ndarray(self):
        return np.ones((480, 2), dtype=np.int16) * 8192


class FakeChannelList:
    channels = [object(), object()]


class FakeSinglePlanePackedStereoAudioFrame:
    sample_rate = 48000
    samples = 480
    layout = FakeChannelList()

    def to_ndarray(self):
        return np.ones((1, 960), dtype=np.int16) * 8192


class ApiWebRTCTests(unittest.TestCase):
    def build_client(self, webrtc=None) -> tuple[TestClient, FakeWebRTCManager | None]:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        manager = webrtc
        app = create_app(
            EventStore(max_context_events=20),
            CallRegistry(),
            AgentTaskTracker(),
            WebSocketHub(),
            TranscriptStore(directory.name),
            None,
            webrtc=manager,
        )
        return TestClient(app), manager

    def test_webrtc_sessions_requires_configured_transport(self) -> None:
        client, _manager = self.build_client()

        response = client.get("/webrtc/sessions")

        self.assertEqual(response.status_code, 503)

    def test_create_webrtc_session_returns_answer(self) -> None:
        client, manager = self.build_client(FakeWebRTCManager())

        response = client.post(
            "/webrtc/sessions",
            json={"sdp": "offer-sdp", "type": "offer", "metadata": {"tenant_id": "tenant-1"}},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["answer"], {"sdp": "answer-sdp", "type": "answer"})
        self.assertEqual(manager.created, [("offer-sdp", "offer", {"tenant_id": "tenant-1"})])

    def test_create_webrtc_session_rejects_non_offer_type(self) -> None:
        client, _manager = self.build_client(FakeWebRTCManager())

        response = client.post("/webrtc/sessions", json={"sdp": "answer-sdp", "type": "answer"})

        self.assertEqual(response.status_code, 400)

    def test_delete_webrtc_session_closes_manager_session(self) -> None:
        client, manager = self.build_client(FakeWebRTCManager())

        response = client.delete("/webrtc/sessions/session-1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"closed": True, "session_id": "session-1"})
        self.assertEqual(manager.closed, ["session-1"])

    def test_delete_webrtc_session_returns_404_for_unknown_session(self) -> None:
        client, _manager = self.build_client(FakeWebRTCManager())

        response = client.delete("/webrtc/sessions/missing")

        self.assertEqual(response.status_code, 404)

    def test_webrtc_audio_is_resampled_to_stt_sample_rate(self) -> None:
        audio = audio_frame_to_call_audio(FakeAudioFrame())

        self.assertEqual(len(audio), 160)
        self.assertEqual(STT_SAMPLE_RATE, 16000)

    def test_webrtc_audio_handles_packed_stereo_frames(self) -> None:
        audio = audio_frame_to_call_audio(FakePackedStereoAudioFrame())

        self.assertEqual(len(audio), 160)
        self.assertAlmostEqual(float(audio.mean()), 0.25, delta=0.02)

    def test_webrtc_audio_handles_single_plane_packed_stereo_frames(self) -> None:
        audio = audio_frame_to_call_audio(FakeSinglePlanePackedStereoAudioFrame())

        self.assertEqual(len(audio), 160)
        self.assertAlmostEqual(float(audio.mean()), 0.25, delta=0.02)


if __name__ == "__main__":
    unittest.main()
